"""Get information for installed and online packages."""

from __future__ import annotations

import configparser
import contextlib
import re
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from email.message import Message
from importlib import metadata
from importlib.metadata._meta import PackageMetadata
from pathlib import Path
from typing import Any

import license_expression
import requests
import requirements
import tomli
from boolean.boolean import Expression
from depgather.models.pypijson import ProjectResponse
from depgather.parse import gather
from license_expression import Licensing
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from licensecheck.models.constants import JOINS, UNKNOWN
from licensecheck.models.packageinfo import PackageInfo
from licensecheck.session import session

RAW_JOINS = " AND "
HTTP_OK = 200
HTTP_NOT_FOUND = 404


def _parse_uv_requirements(raw_requirements: str, skip_dependencies: set[str]) -> set[Requirement]:
	skip_names = {canonicalize_name(name) for name in skip_dependencies}
	parsed_requirements: set[Requirement] = set()

	for parsed in requirements.parse(raw_requirements):
		if parsed.editable:
			continue
		if not parsed.name or canonicalize_name(parsed.name) in skip_names:
			continue

		requirement = Requirement(parsed.line)
		requirement.name = canonicalize_name(requirement.name)
		parsed_requirements.add(requirement)

	return parsed_requirements


def _gather_uv_requirements(
	requirements_path: Path,
	groups: set[str],
	extras: set[str],
	skip_dependencies: set[str],
	base_index_url: str,
) -> set[Requirement]:
	lock_path = requirements_path.with_name("uv.lock")
	use_lock = requirements_path.name == "pyproject.toml" and lock_path.is_file()
	if use_lock:
		command = [
			"uv",
			"export",
			"--project",
			requirements_path.parent.as_posix(),
			"--format",
			"requirements.txt",
			"--locked",
			"--no-hashes",
			"--no-header",
			"--no-default-groups",
			"--no-emit-project",
			"--color",
			"never",
		]
	else:
		command = [
			"uv",
			"pip",
			"compile",
			"--color",
			"never",
			"--index",
			base_index_url,
			requirements_path.as_posix(),
		]
	for group in groups:
		command.extend(["--group", group])
	for extra in extras:
		command.extend(["--extra", extra])

	if not use_lock and requirements_path.name == "pyproject.toml":
		pyproject = tomli.loads(requirements_path.read_text(encoding="utf-8"))
		prerelease = pyproject.get("tool", {}).get("uv", {}).get("prerelease")
		if prerelease:
			command.extend(["--prerelease", prerelease])

	try:
		result = subprocess.run(  # noqa: S603
			command,
			capture_output=True,
			text=True,
			check=False,
		)
	except OSError as error:
		raise RuntimeError from error

	if result.returncode != 0:
		message = f"Non-zero returncode: {result.stderr}, {result.stdout}"
		raise RuntimeError(message)

	return _parse_uv_requirements(result.stdout, skip_dependencies)


class PackageInfoManager:
	"""Manages retrieval of local and remote package information."""

	def __init__(self, base_pypi_url: str = "https://pypi.org") -> None:
		"""
		Manage retrieval of local and remote package information.

		:param str pypi_api: url of pypi server. Typically the public instance, defaults
		to "https://pypi.org"
		"""
		self.base_pypi_url = base_pypi_url
		self.reqs: set[Requirement] = set()
		self.local_projects: dict[str, PackageInfo] = {}

	def resolve_requirements(
		self,
		requirements_paths: set[str],
		groups: set[str],
		extras: set[str],
		skip_dependencies: set[str],
	) -> None:
		for requirements_path in requirements_paths:
			requirements_path_obj = Path(requirements_path)
			self._register_local_sources(requirements_path_obj)
			try:
				resolved_requirements = _gather_uv_requirements(
					requirements_path=requirements_path_obj,
					groups=groups,
					extras=extras,
					skip_dependencies=skip_dependencies,
					base_index_url=self.base_pypi_url,
				)
			except RuntimeError:
				if requirements_path_obj.name == "pyproject.toml":
					raise
				resolved_requirements = gather(
					skipDependencies=skip_dependencies,
					groups=groups,
					extras=extras,
					requirementsPath=requirements_path_obj,
					base_index_url=self.base_pypi_url,
				)

			self.reqs.update(resolved_requirements)

	def _register_local_sources(
		self,
		pyproject_path: Path,
		seen: set[Path] | None = None,
	) -> None:
		if pyproject_path.name != "pyproject.toml" or not pyproject_path.is_file():
			return

		resolved_path = pyproject_path.resolve()
		seen = seen or set()
		if resolved_path in seen:
			return
		seen.add(resolved_path)

		pyproject = tomli.loads(pyproject_path.read_text(encoding="utf-8"))
		uv_config = pyproject.get("tool", {}).get("uv", {})
		source_paths: set[Path] = set()

		for source in uv_config.get("sources", {}).values():
			source_options = source if isinstance(source, list) else [source]
			for source_option in source_options:
				if not isinstance(source_option, dict) or "path" not in source_option:
					continue
				source_path = pyproject_path.parent / source_option["path"]
				source_paths.add(
					source_path
					if source_path.name == "pyproject.toml"
					else source_path / "pyproject.toml"
				)

		for member_pattern in uv_config.get("workspace", {}).get("members", []):
			for member_path in pyproject_path.parent.glob(member_pattern):
				source_paths.add(member_path / "pyproject.toml")

		for source_path in source_paths:
			source_package = self._read_project_package(source_path)
			if source_package is not None:
				self.local_projects[source_package.name] = source_package
			self._register_local_sources(source_path, seen)

	@staticmethod
	def _read_project_package(pyproject_path: Path) -> PackageInfo | None:
		if not pyproject_path.is_file():
			return None

		pyproject = tomli.loads(pyproject_path.read_text(encoding="utf-8"))
		project = pyproject.get("project", {})
		name = project.get("name")
		if not name:
			return None

		license_value = project.get("license", UNKNOWN)
		if isinstance(license_value, dict):
			license_value = license_value.get("text", UNKNOWN)

		authors = project.get("authors", [])
		author_names = [
			author.get("name", "") if isinstance(author, dict) else str(author)
			for author in authors
		]
		project_urls = project.get("urls", {})

		return PackageInfo(
			name=canonicalize_name(name),
			version=project.get("version"),
			homePage=project_urls.get("Homepage") or project_urls.get("homepage"),
			author=", ".join(filter(None, author_names)),
			license=str(license_value),
			errorCode=0,
		)

	def getPackages(self) -> set[PackageInfo]:
		"""
		Retrieve package information from local installation or PyPI.

		:param set[str] reqs: Set of dependency names to retrieve information for.
		:return set[PackageInfo]: A set of package information objects.
		"""
		with ThreadPoolExecutor() as executor:
			return set(executor.map(self._get_package_info, self.reqs))

	def _get_package_info(self, package: Requirement) -> PackageInfo:
		"""
		Retrieve package information, preferring local info.

		:param Requirement package: package info to unpack
		:return PackageInfo: Information about the package.
		"""
		versions: set[str | None] = {None}
		package.name = canonicalize_name(package.name)

		if local_project := self.local_projects.get(package.name):
			return replace(local_project)

		specifier = getattr(package, "specifier", None)
		if specifier is not None:
			parsed_versions = {
				item.version
				for item in specifier
				if item.operator in {"==", "==="} and "*" not in item.version
			}
			if parsed_versions:
				versions = parsed_versions

		package.name = canonicalize_name(package.name)

		base_pkg_info: PackageInfo = PackageInfo(
			name=package.name, version=versions.pop(), errorCode=1
		)

		lpi = LocalPackageInfo(package=base_pkg_info)
		rpi = RemotePackageInfo(pypi_api=self.base_pypi_url, package=base_pkg_info)
		rpi.lazy_fetch()

		ipi = IndexPackageInfo(package=base_pkg_info) if rpi.http_code == HTTP_NOT_FOUND else None
		index_name = ipi.get_name() if ipi is not None else None

		pkg_info = PackageInfo(
			name=package.name,
			version=base_pkg_info.version
			or lpi.get_version()
			or (ipi.get_version() if ipi is not None else None)
			or rpi.get_version(),
			size=lpi.get_size() or (ipi.get_size() if ipi is not None else None) or rpi.get_size(),
			homePage=lpi.get_homePage()
			or (ipi.get_homePage() if ipi is not None else None)
			or rpi.get_homePage(),
			author=lpi.get_author()
			or (ipi.get_author() if ipi is not None else None)
			or rpi.get_author(),
			license=str(
				lpi.get_license()
				or (ipi.get_license() if ipi is not None else None)
				or rpi.get_license()
			),
			errorCode=(
				0 if rpi.http_code == HTTP_OK or lpi.get_name() or index_name else rpi.http_code
			),
		)

		# normailzing the license
		if pkg_info.license:
			pkg_info.license = normalize_license(pkg_info.license)

		return pkg_info


def normalize_license(lice: str) -> str:
	licensing = Licensing()
	parsed = None
	with contextlib.suppress(license_expression.ExpressionParseError):
		parsed = licensing.parse(re.sub(r"[^a-zA-Z0-9_.:\- ]", "_", lice.splitlines()[0]))
	if parsed is None:
		return lice

	tokens: list[Expression] = sorted(parsed.literals)
	return str(JOINS.join(getattr(x, "key", str(x)) for x in tokens))


class LocalPackageInfo:
	"""Handles retrieval of package info from local installation."""

	def __init__(self, package: PackageInfo) -> None:
		self.package: PackageInfo = package
		# email message appears to mostly conform to the protocol
		# https://packaging.python.org/en/latest/specifications/core-metadata/#core-metadata
		self.meta: PackageMetadata = Message()
		with contextlib.suppress(metadata.PackageNotFoundError):
			self.meta = metadata.metadata(package.name)

	def get_license(self) -> str | None:
		return (
			self.meta.get("License-Expression")
			or from_classifiers(self.meta.get_all("Classifier"))
			or self.meta.get("License")
		)

	def get_name(self) -> str | None:
		return self.meta.get("Name")

	def get_version(self) -> str | None:
		return self.meta.get("Version")

	def get_homePage(self) -> str | None:
		return self.meta.get("Home-page")

	def get_author(self) -> str | None:
		return self.meta.get("Author")

	def get_size(self) -> int | None:
		"""
		Retrieve installed package size.

		:param str package: Package name.
		:return int: Size in bytes.
		"""
		try:
			package_files = metadata.Distribution.from_name(self.package.name).files
			return sum(f.size for f in package_files if f.size) if package_files else 0
		except metadata.PackageNotFoundError:
			return None  # Package not found


class IndexPackageInfo:
	"""Handles package metadata from indexes configured for uv."""

	def __init__(self, package: PackageInfo) -> None:
		self.package = package
		self.meta: PackageMetadata = Message()
		self.fetched = False

	def lazy_fetch(self) -> None:
		if self.fetched:
			return
		self.fetched = True

		requirement = self.package.name
		if self.package.version:
			requirement = f"{requirement}=={self.package.version}"

		with tempfile.TemporaryDirectory(prefix="licensecheck-") as target:
			command = [
				"uv",
				"pip",
				"install",
				"--color",
				"never",
				"--no-progress",
				"--no-deps",
				"--only-binary",
				":all:",
				"--target",
				target,
				requirement,
			]
			try:
				result = subprocess.run(  # noqa: S603
					command,
					capture_output=True,
					text=True,
					check=False,
				)
			except OSError:
				return

			if result.returncode != 0:
				return

			for distribution in metadata.distributions(path=[target]):
				name = distribution.metadata.get("Name")
				if name and canonicalize_name(name) == self.package.name:
					self.meta = distribution.metadata
					return

	def get_license(self) -> str | None:
		self.lazy_fetch()
		return (
			self.meta.get("License-Expression")
			or from_classifiers(self.meta.get_all("Classifier"))
			or self.meta.get("License")
		)

	def get_name(self) -> str | None:
		self.lazy_fetch()
		return self.meta.get("Name")

	def get_version(self) -> str | None:
		self.lazy_fetch()
		return self.meta.get("Version")

	def get_homePage(self) -> str | None:
		self.lazy_fetch()
		return self.meta.get("Home-page")

	def get_author(self) -> str | None:
		self.lazy_fetch()
		return self.meta.get("Author")

	def get_size(self) -> None:
		return None


class RemotePackageInfo:
	"""Handles retrieval of package info from PyPI."""

	def __init__(self, pypi_api: str, package: PackageInfo) -> None:
		self.pypi_api_pypi = pypi_api + "/pypi"
		self.pypi_api_integrity = pypi_api + "/integrity"
		self.package = package
		self.http_code: int = 0
		self.resp: ProjectResponse = None

	def lazy_fetch(self) -> None:
		if self.resp is None:
			if self.package.version:
				rc, raw_resp = self.make_req(
					url=f"{self.pypi_api_pypi}/{self.package.name}/{self.package.version}/json"
				)
			else:
				rc, raw_resp = self.make_req(url=f"{self.pypi_api_pypi}/{self.package.name}/json")

			self.http_code = rc
			self.resp = ProjectResponse.model_validate(raw_resp)

	def make_req(
		self, url: str, headers: dict[str, str] | None = None
	) -> tuple[int, dict[str, Any]]:
		headers = headers or {}
		try:
			r = session.get(url, headers=headers, timeout=60)

			return r.status_code, r.json()
		except requests.exceptions.JSONDecodeError:
			return -1, {}
		except requests.exceptions.RequestException:
			return -2, {}

	def get_name(self) -> str:
		self.lazy_fetch()
		return self.resp.info.name

	def get_version(self) -> str:
		self.lazy_fetch()
		return self.resp.info.version

	def get_homePage(self) -> str:
		self.lazy_fetch()
		return self.resp.info.home_page

	def get_author(self) -> str:
		self.lazy_fetch()
		author_email = self.resp.info.author_email or ""
		return self.resp.info.author or author_email.split("<")[0].strip()

	def get_license(self) -> str:
		self.lazy_fetch()
		return (
			self.resp.info.license_expression
			or from_classifiers(self.resp.info.classifiers)
			or self.resp.info.license
		)

	def get_size(self) -> int | None:
		self.lazy_fetch()
		urls = self.resp.urls
		return urls[-1].size if len(urls) > 0 else None


def from_classifiers(classifiers: list[str] | None) -> str | None:
	"""
	Extract license from classifiers.

	:param list[str] | None classifiers: list of classifiers
	:return str: licenses as a str
	"""
	if not classifiers:
		return None

	licenses: list[str] = []
	for _val in classifiers:
		val = str(_val)
		if val.startswith("License"):
			lice = val.rsplit(" :: ", maxsplit=1)[-1]
			if lice != "OSI Approved":
				licenses.append(lice)
	return RAW_JOINS.join(licenses) if len(licenses) > 0 else None


class ProjectMetadata:
	"""Handles extraction of project metadata from configuration files."""

	@staticmethod
	def get_metadata() -> dict[str, Any]:
		"""
		Extract project metadata from setup.cfg or pyproject.toml.

		:return dict[str, Any]: Extracted metadata.
		"""
		if Path("setup.cfg").exists():
			config = configparser.ConfigParser()
			config.read("setup.cfg")
			if "metadata" in config:
				classifiers = config.get("metadata", "classifier", fallback="").strip().splitlines()
				license_str = str(config.get("metadata", "license", fallback=""))
				return {"classifiers": classifiers, "license": license_str}

		if Path("pyproject.toml").exists():
			pyproject = tomli.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
			tool = pyproject.get("tool", {})
			return (
				pyproject.get("project", {})
				or tool.get("poetry")
				or tool.get("flit", {}).get("metadata", {})
			)

		return {"classifiers": [], "license": UNKNOWN}

	@staticmethod
	def get_license() -> str:
		"""
		Extract license from project metadata.

		:return str: License string.
		"""
		metadata = ProjectMetadata.get_metadata()
		license_str = from_classifiers(metadata.get("classifiers", []))

		if license_str is not None:
			return str(license_str)

		if isinstance(metadata.get("license"), dict):
			return str(metadata["license"].get("text", UNKNOWN))

		return str(metadata.get("license", UNKNOWN))
