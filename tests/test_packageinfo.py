from __future__ import annotations

from pathlib import Path

import pytest
from packaging.requirements import Requirement

from licensecheck.models.constants import UNKNOWN
from licensecheck.models.packageinfo import PackageInfo
from licensecheck.packageinforesolver import (
	LocalPackageInfo,
	PackageInfoManager,
	RemotePackageInfo,
	from_classifiers,
	normalize_license,
)

THISDIR = str(Path(__file__).resolve().parent)


@pytest.fixture
def package_info_manager() -> PackageInfoManager:
	"""Fixture to provide a PackageInfoManager instance."""
	return PackageInfoManager("https://pypi.org/")


@pytest.fixture
def local_package_info() -> LocalPackageInfo:
	return LocalPackageInfo(requests_package)


@pytest.fixture
def remote_package_info() -> RemotePackageInfo:
	return RemotePackageInfo("https://pypi.org/", requests_package)


def aux_packageinfo(package_name: str) -> PackageInfo:
	return PackageInfo(name=package_name)


requests_package = aux_packageinfo("requests")


def test_getPackageInfoLocal(local_package_info: LocalPackageInfo) -> None:
	try:
		name = local_package_info.get_name()
		assert name == "requests"
	except ModuleNotFoundError:
		assert True


def test_getPackageInfoPypi(remote_package_info: RemotePackageInfo) -> None:
	pkg = remote_package_info

	assert pkg.get_name() == "requests"
	assert pkg.get_author() == "Kenneth Reitz"
	assert pkg.get_license() == "Apache Software License"


def test_getPackageInfoLocalNotFound() -> None:
	pkg = LocalPackageInfo(aux_packageinfo("this_package_does_not_exist"))
	assert pkg.get_size() is None


def test_getPackagePypiLocalNotFound() -> None:
	pkg = RemotePackageInfo("https://pypi.org/", aux_packageinfo("this_package_does_not_exist"))
	assert pkg.get_size() is None


def test_getPackages(package_info_manager: PackageInfoManager) -> None:
	package_info_manager.reqs = {aux_packageinfo("requests")}
	packages = package_info_manager.getPackages()
	package = packages.pop()
	assert package.name == "requests"
	assert package.author == "Kenneth Reitz"
	assert package.license == "Apache Software License"


def test_getPackagesNotFound(package_info_manager: PackageInfoManager) -> None:
	package_info_manager.reqs = {aux_packageinfo("this_package_does_not_exist")}

	packages = package_info_manager.getPackages()
	package = packages.pop()

	assert package.name == "this-package-does-not-exist"
	assert package.errorCode == 404


def test_from_classifiers() -> None:
	lines = Path(f"{THISDIR}/data/pypiClassifiers.txt").read_text("utf-8").splitlines()
	licenses = [from_classifiers([rawLicense]) or UNKNOWN for rawLicense in lines]
	# Path(f"{THISDIR}/data/licenses.txt").write_text("\n".join(licenses), "utf-8")
	assert "\n".join(licenses) == Path(f"{THISDIR}/data/licenses.txt").read_text("utf-8")


def test_licenseFromEmptyClassifierlist() -> None:
	licenses = []
	licenses.append(from_classifiers([]))
	assert licenses == [None]


def test_getModuleSize() -> None:
	local_package_info = LocalPackageInfo(aux_packageinfo("this_package_does_not_exist"))
	local_package_info.get_size()


@pytest.mark.parametrize(
	("lice", "normalized"),
	[
		("mit", "mit"),
		("BSD-2-Clause", "BSD-2-Clause"),
		("BSD-2-Clause AND Apache-2.0", "Apache-2.0;; BSD-2-Clause"),
		(
			"BSD-2-Clause AND Apache-2.0 WITH LLVM-exception",
			"Apache-2.0 WITH LLVM-exception;; BSD-2-Clause",
		),
	],
)
def test_normalize_license(lice: str, normalized: str) -> None:
	assert normalize_license(lice) == normalized


def test_unpinned_requirement_does_not_crash(package_info_manager: PackageInfoManager) -> None:
	package_info_manager.reqs = {Requirement("sample")}

	packages = package_info_manager.getPackages()
	package: PackageInfo = packages.pop()

	assert package.name == "sample"
	assert package.errorCode == 0


def test_resolve_requirements_handles_nested_editable_uv_sources(
	package_info_manager: PackageInfoManager, tmp_path: Path
) -> None:
	transitive_path = tmp_path / "transitive_dependency"
	transitive_path.mkdir()
	(transitive_path / "pyproject.toml").write_text(
		"""
[project]
name = "transitive-dependency"
version = "1.0.0"
""".strip(),
		encoding="utf-8",
	)

	nested_path = tmp_path / "nested_workspace_member"
	nested_path.mkdir()
	(nested_path / "pyproject.toml").write_text(
		"""
[project]
name = "nested-dependency"
version = "1.0.0"
""".strip(),
		encoding="utf-8",
	)

	dependency_path = tmp_path / "workspace_member"
	dependency_path.mkdir()
	(dependency_path / "pyproject.toml").write_text(
		f"""
[project]
name = "local-dependency"
version = "1.0.0"
dependencies = [
    "nested-dependency",
    "transitive-dependency @ {transitive_path.as_uri()}",
]

[tool.uv.sources]
nested-dependency = {{ path = "../nested_workspace_member", editable = true }}
""".strip(),
		encoding="utf-8",
	)

	project_path = tmp_path / "project"
	project_path.mkdir()
	pyproject_path = project_path / "pyproject.toml"
	pyproject_path.write_text(
		"""
[project]
name = "project"
version = "1.0.0"
dependencies = ["local-dependency"]

[tool.uv.sources]
local-dependency = { path = "../workspace_member", editable = true }
""".strip(),
		encoding="utf-8",
	)

	package_info_manager.resolve_requirements(
		requirements_paths={str(pyproject_path)},
		groups=set(),
		extras=set(),
		skip_dependencies=set(),
	)

	assert {requirement.name for requirement in package_info_manager.reqs} == {
		"transitive-dependency"
	}


def test_resolve_requirements_handles_editable_uv_sources_in_monorepo(
	package_info_manager: PackageInfoManager, tmp_path: Path
) -> None:
	repository_path = tmp_path / "repository"
	libraries_path = repository_path / "libs"

	published_path = repository_path / "published_dependency"
	published_path.mkdir(parents=True)
	(published_path / "pyproject.toml").write_text(
		"""
[project]
name = "published-dependency"
version = "1.0.0"
""".strip(),
		encoding="utf-8",
	)

	sdk_path = libraries_path / "sdk_extensions"
	sdk_path.mkdir(parents=True)
	(sdk_path / "pyproject.toml").write_text(
		f"""
[project]
name = "sdk-extensions"
version = "1.0.0"
dependencies = ["published-dependency @ {published_path.as_uri()}"]
""".strip(),
		encoding="utf-8",
	)

	anomaly_path = libraries_path / "anomaly_detection"
	anomaly_path.mkdir()
	(anomaly_path / "pyproject.toml").write_text(
		"""
[project]
name = "anomaly-detection"
version = "1.0.0"
""".strip(),
		encoding="utf-8",
	)

	common_path = libraries_path / "service_common"
	common_path.mkdir()
	(common_path / "pyproject.toml").write_text(
		"""
[project]
name = "service-common"
version = "1.0.0"
dependencies = ["sdk-extensions", "anomaly-detection"]

[tool.uv.sources]
sdk-extensions = { path = "../sdk_extensions", editable = true }
anomaly-detection = { path = "../anomaly_detection", editable = true }
""".strip(),
		encoding="utf-8",
	)

	function_path = repository_path / "functions" / "reconciliation"
	function_path.mkdir(parents=True)
	pyproject_path = function_path / "pyproject.toml"
	pyproject_path.write_text(
		"""
[project]
name = "reconciliation"
version = "1.0.0"
dependencies = ["sdk-extensions", "service-common", "anomaly-detection"]

[tool.uv.sources]
sdk-extensions = { path = "../../libs/sdk_extensions", editable = true }
service-common = { path = "../../libs/service_common", editable = true }
anomaly-detection = { path = "../../libs/anomaly_detection", editable = true }

[tool.uv]
package = false
""".strip(),
		encoding="utf-8",
	)

	package_info_manager.resolve_requirements(
		requirements_paths={str(pyproject_path)},
		groups=set(),
		extras=set(),
		skip_dependencies=set(),
	)

	assert {requirement.name for requirement in package_info_manager.reqs} == {
		"published-dependency"
	}
