# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

import contextlib
import fnmatch
import io
import os
import pathlib
import shutil
import tarfile
import tempfile

from .docker import container_exec, container_get_archive, copy_file_to_container
from .downloads import DOWNLOADS
from .logging import log
from .utils import (
    clang_toolchain,
    create_tar_from_directory,
    exec_and_log,
    extract_tar_to_directory,
    normalize_tar_archive,
)


class ContainerContext(object):
    def __init__(self, container):
        self.container = container

        self.tools_path = "/tools"

    @property
    def is_isolated(self):
        return True

    def copy_file(self, source: pathlib.Path, dest_path=None, dest_name=None):
        dest_name = dest_name or source.name
        dest_path = dest_path or "/build"
        copy_file_to_container(source, self.container, dest_path, dest_name)

    def install_toolchain_archive(self, build_dir, package_name, host_platform):
        entry = DOWNLOADS[package_name]
        basename = f'{package_name}-{entry["version"]}-{host_platform}.tar'

        p = build_dir / basename
        self.copy_file(p)
        self.run(["/bin/tar", "-C", "/tools", "-xf", f"/build/{p.name}"], user="root")

    def install_artifact_archive(
        self, build_dir, package_name, target_triple, optimizations
    ):
        entry = DOWNLOADS[package_name]
        basename = f'{package_name}-{entry["version"]}-{target_triple}-{optimizations}.tar'

        p = build_dir / basename

        self.copy_file(p)
        self.run(["/bin/tar", "-C", "/tools", "-xf", f"/build/{p.name}"], user="root")

    def install_toolchain(
        self,
        build_dir,
        host_platform,
        target_triple: str,
        binutils=False,
        musl=False,
        clang=False,
    ):
        if binutils:
            self.install_toolchain_archive(build_dir, "binutils", host_platform)

        if clang:
            self.install_toolchain_archive(
                build_dir, clang_toolchain(host_platform, target_triple), host_platform
            )

        if musl:
            self.install_toolchain_archive(build_dir, "musl", host_platform)

    def run(self, program, user="build", environment=None):
        if isinstance(program, str) and not program.startswith("/"):
            program = f"/build/{program}"

        container_exec(self.container, program, user=user, environment=environment)

    def get_tools_archive(self, dest, name):
        log(f"copying container files to {dest}")
        data = container_get_archive(self.container, f"/build/out/tools/{name}")

        with open(dest, "wb") as fh:
            fh.write(data)

    def get_file(self, path):
        log(f"retrieving container file {path}")
        data = io.BytesIO(container_get_archive(self.container, f"/build/{path}"))

        with tarfile.open(fileobj=data) as tf:
            for ti in tf:
                return tf.extractfile(ti).read()

        raise Exception("file not found")

    def get_output_archive(self, path=None, as_tar=False):
        p = "/build/out"
        if path:
            p += f"/{path}"

        data = container_get_archive(self.container, p)
        data = io.BytesIO(data)

        data = normalize_tar_archive(data)

        return tarfile.open(fileobj=data) if as_tar else data.getvalue()

    def find_output_files(self, base_path, pattern):
        command = ["/usr/bin/find", f"/build/out/{base_path}", "-name", pattern]

        for line in self.container.exec_run(command, user="build")[1].splitlines():
            if not line.strip():
                continue

            yield line[len(f"/build/out/{base_path}/"):].decode("ascii")


class TempdirContext(object):
    def __init__(self, td):
        self.td = pathlib.Path(td)

        self.tools_path = str(self.td / "tools")

    @property
    def is_isolated(self):
        return False

    def copy_file(self, source: pathlib.Path, dest_path=None, dest_name=None):
        dest_dir = self.td / dest_path if dest_path else self.td
        dest_dir.mkdir(exist_ok=True)

        dest_name = dest_name or source.name
        log(f"copying {source} to {dest_dir}/{dest_name}")
        shutil.copy(source, dest_dir / dest_name)

    def install_toolchain_archive(self, build_dir, package_name, host_platform):
        entry = DOWNLOADS[package_name]
        basename = f'{package_name}-{entry["version"]}-{host_platform}.tar'

        p = build_dir / basename
        dest_path = self.td / "tools"
        log(f"extracting {p} to {dest_path}")
        extract_tar_to_directory(p, dest_path)

    def install_artifact_archive(
        self, build_dir, package_name, target_triple, optimizations
    ):
        entry = DOWNLOADS[package_name]
        basename = f'{package_name}-{entry["version"]}-{target_triple}-{optimizations}.tar'

        p = build_dir / basename
        dest_path = self.td / "tools"
        log(f"extracting {p} to {dest_path}")
        extract_tar_to_directory(p, dest_path)

    def install_toolchain(
        self,
        build_dir,
        platform,
        target_triple,
        binutils=False,
        musl=False,
        clang=False,
    ):
        if binutils:
            self.install_toolchain_archive(build_dir, "binutils", platform)

        if clang:
            self.install_toolchain_archive(
                build_dir, clang_toolchain(platform, target_triple), platform
            )

        if musl:
            self.install_toolchain_archive(build_dir, "musl", platform)

    def run(self, program, user="build", environment=None):
        if user != "build":
            raise Exception("cannot change user in temp directory builds")

        if isinstance(program, str) and not program.startswith("/"):
            program = str(self.td / program)

        exec_and_log(program, cwd=self.td, env=environment)

    def get_tools_archive(self, dest, name):
        log(f"copying built files to {dest}")

        with dest.open("wb") as fh:
            create_tar_from_directory(fh, self.td / "out" / "tools")

    def get_file(self, path):
        log(f"retrieving file {path}")

        p = self.td / path
        with p.open("rb") as fh:
            return fh.read()

    def get_output_archive(self, path, as_tar=False):
        p = self.td / "out" / path

        data = io.BytesIO()
        create_tar_from_directory(data, p, path_prefix=p.parts[-1])
        data.seek(0)

        data = normalize_tar_archive(data)

        return tarfile.open(fileobj=data) if as_tar else data.getvalue()

    def find_output_files(self, base_path, pattern):
        base = str(self.td / "out" / base_path)

        for root, dirs, files in os.walk(base):
            dirs.sort()

            for f in sorted(files):
                if fnmatch.fnmatch(f, pattern):
                    full = os.path.join(root, f)
                    yield full[len(base) + 1 :]


@contextlib.contextmanager
def build_environment(client, image):
    if client is not None:
        container = client.containers.run(
            image, command=["/bin/sleep", "86400"], detach=True
        )
        td = None
        context = ContainerContext(container)
    else:
        container = None
        td = tempfile.TemporaryDirectory()
        context = TempdirContext(td.name)

    try:
        yield context
    finally:
        if container:
            container.stop(timeout=0)
            container.remove()
        else:
            td.cleanup()
