# Copyright (c) 2023 Airbyte, Inc., all rights reserved.
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

import requests
import yaml
from requests import HTTPError
from rich import print  # noqa: A004  # Allow shadowing the built-in

from airbyte import exceptions as exc
from airbyte._executors.declarative import DeclarativeExecutor
from airbyte._executors.docker import DockerExecutor
from airbyte._executors.local import PathExecutor
from airbyte._executors.python import VenvExecutor
from airbyte._util.meta import which
from airbyte._util.telemetry import EventState, log_install_state  # Non-public API
from airbyte.sources.registry import ConnectorMetadata, InstallType, get_connector_metadata


if TYPE_CHECKING:
    from airbyte._executors.base import Executor


def _try_get_source_manifest(source_name: str, manifest_url: str | None) -> dict:
    """Try to get a source manifest from a URL.

    If the URL is not provided, we'll try a couple of default URLs.
    We can remove/refactor this once manifests are available in GCS connector registry.
    """
    if manifest_url:
        response = requests.get(url=manifest_url)
        response.raise_for_status()  # Raise HTTPError exception if the download failed
        try:
            return cast(dict, yaml.safe_load(response.text))
        except yaml.YAMLError as ex:
            raise exc.AirbyteConnectorInstallationError(
                message="Failed to parse the connector manifest YAML.",
                connector_name=source_name,
                context={
                    "manifest_url": manifest_url,
                },
            ) from ex

    # No manifest URL was provided. We'll try a couple of default URLs.

    try:
        # First try the new URL format (language='manifest-only'):
        result_1 = _try_get_source_manifest(
            source_name=source_name,
            manifest_url=(
                f"https://raw.githubusercontent.com/airbytehq/airbyte/master/airbyte-integrations"
                f"/connectors/{source_name}/manifest.yaml"
            ),
        )
    except HTTPError as ex_1:
        # If the new URL path was not found, try the old URL format (language='low-code'):
        try:
            result_2 = _try_get_source_manifest(
                source_name=source_name,
                manifest_url=(
                    f"https://raw.githubusercontent.com/airbytehq/airbyte/master/airbyte-integrations"
                    f"/connectors/{source_name}/{source_name.replace('-', '_')}/manifest.yaml"
                ),
            )
        except HTTPError:
            # Raise the first exception, since that represents the new default URL
            raise ex_1 from None
        else:
            # Old URL path was found (no exceptions raised).
            return result_2
    else:
        # New URL path was found (no exceptions raised).
        return result_1


def _get_local_executor(
    name: str,
    local_executable: Path | str | Literal[True],
    version: str | None,
) -> Executor:
    """Get a local executor for a connector."""
    if version:
        raise exc.PyAirbyteInputError(
            message="Param 'version' is not supported when 'local_executable' is set."
        )

    if local_executable is True:
        # Use the default executable name for the connector
        local_executable = name

    if isinstance(local_executable, str):
        if "/" in local_executable or "\\" in local_executable:
            # Assume this is a path
            local_executable = Path(local_executable).absolute()
        else:
            which_executable: Path | None = which(local_executable)
            if not which_executable:
                raise exc.AirbyteConnectorExecutableNotFoundError(
                    connector_name=name,
                    context={
                        "executable": name,
                        "working_directory": Path.cwd().absolute(),
                    },
                ) from FileNotFoundError(name)
            local_executable = Path(which_executable).absolute()

    # `local_executable` is now a Path object

    print(f"Using local `{name}` executable: {local_executable!s}")
    return PathExecutor(
        name=name,
        path=local_executable,
    )


def get_connector_executor(  # noqa: PLR0912, PLR0913 # Too complex
    name: str,
    *,
    version: str | None = None,
    pip_url: str | None = None,
    local_executable: Path | str | None = None,
    docker_image: bool | str | None = None,
    use_host_network: bool = False,
    source_manifest: bool | dict | Path | str | None = None,
    install_if_missing: bool = True,
    install_root: Path | None = None,
) -> Executor:
    """This factory function creates an executor for a connector.

    For documentation of each arg, see the function `airbyte.sources.util.get_source()`.
    """
    install_method_count = sum(
        [
            bool(local_executable),
            bool(docker_image),
            bool(pip_url),
            bool(source_manifest),
        ]
    )
    if install_method_count > 1:
        raise exc.PyAirbyteInputError(
            message=(
                "You can only specify one of the settings: 'local_executable', 'docker_image', "
                "'source_manifest', or 'pip_url'."
            ),
            context={
                "local_executable": local_executable,
                "docker_image": docker_image,
                "pip_url": pip_url,
                "source_manifest": source_manifest,
            },
        )
    metadata: ConnectorMetadata | None = None
    try:
        metadata = get_connector_metadata(name)
    except exc.AirbyteConnectorNotRegisteredError as ex:
        if install_method_count == 0:
            # User has not specified how to install the connector, and it is not registered.
            # Fail the install.
            log_install_state(name, state=EventState.FAILED, exception=ex)
            raise

    if install_method_count == 0:
        # User has not specified how to install the connector.
        # Prefer local executable if found, then manifests, then python, then docker, depending upon
        # how the connector is declared in the connector registry.
        if which(name):
            local_executable = name
        elif metadata and metadata.install_types:
            match metadata.default_install_type:
                case InstallType.YAML:
                    source_manifest = True
                case InstallType.PYTHON:
                    pip_url = metadata.pypi_package_name
                case _:
                    docker_image = True

    if local_executable:
        return _get_local_executor(
            name=name,
            local_executable=local_executable,
            version=version,
        )

    if docker_image:
        if docker_image is True:
            # Use the default image name for the connector
            docker_image = f"airbyte/{name}"

        if version is not None and ":" in docker_image:
            raise exc.PyAirbyteInputError(
                message="The 'version' parameter is not supported when a tag is already set in the "
                "'docker_image' parameter.",
                context={
                    "docker_image": docker_image,
                    "version": version,
                },
            )

        if ":" not in docker_image:
            docker_image = f"{docker_image}:{version or 'latest'}"

        temp_dir = tempfile.gettempdir()
        local_mount_dir = Path().absolute() / name
        local_mount_dir.mkdir(exist_ok=True)

        docker_cmd = [
            "docker",
            "run",
            "--rm",
            "-i",
            "--volume",
            f"{local_mount_dir}:/local/",
            "--volume",
            f"{temp_dir}:{temp_dir}",
        ]

        if use_host_network is True:
            docker_cmd.extend(["--network", "host"])

        docker_cmd.extend([docker_image])

        return DockerExecutor(
            name=name,
            executable=docker_cmd,
        )

    if source_manifest:
        if isinstance(source_manifest, dict | Path):
            return DeclarativeExecutor(
                name=name,
                manifest=source_manifest,
            )

        if isinstance(source_manifest, str | bool):
            # Source manifest is either a URL or a boolean (True)
            source_manifest = _try_get_source_manifest(
                source_name=name,
                manifest_url=None if source_manifest is True else source_manifest,
            )

            return DeclarativeExecutor(
                name=name,
                manifest=source_manifest,
            )

    # else: we are installing a connector in a Python virtual environment:

    try:
        executor = VenvExecutor(
            name=name,
            metadata=metadata,
            target_version=version,
            pip_url=pip_url,
            install_root=install_root,
        )
        if install_if_missing:
            executor.ensure_installation()

    except Exception as e:
        log_install_state(name, state=EventState.FAILED, exception=e)
        raise
    else:
        # No exceptions were raised, so return the executor.
        return executor
