#!/usr/bin/env python3
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

import argparse
import os
import multiprocessing
import pathlib
import platform
import subprocess
import sys

from pythonbuild.downloads import DOWNLOADS
from pythonbuild.utils import (
    compress_python_archive,
    get_target_settings,
    release_tag_from_git,
    supported_targets,
)

ROOT = pathlib.Path(os.path.abspath(__file__)).parent.parent
BUILD = ROOT / "build"
DIST = ROOT / "dist"
SUPPORT = ROOT / "cpython-unix"
TARGETS_CONFIG = SUPPORT / "targets.yml"


def main():
    if sys.platform == "darwin":
        host_platform = "macos"
        machine = platform.machine()

        if machine == "arm64":
            default_target_triple = "aarch64-apple-darwin"
        elif machine == "x86_64":
            default_target_triple = "x86_64-apple-darwin"
        else:
            raise Exception(f"unhandled macOS machine value: {machine}")
    elif sys.platform == "linux":
        host_platform = "linux64"
        default_target_triple = "x86_64-unknown-linux-gnu"
    else:
        print(f"unsupport build platform: {sys.platform}")
        return 1

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--target-triple",
        default=default_target_triple,
        choices=supported_targets(TARGETS_CONFIG),
        help="Target host triple to build for",
    )

    parser.add_argument(
        "--optimizations",
        choices={"debug", "noopt", "pgo", "lto", "pgo+lto"},
        default="noopt",
        help="Optimizations to apply when compiling Python",
    )
    parser.add_argument(
        "--python",
        choices={"cpython-3.8", "cpython-3.9", "cpython-3.10", "cpython-3.11"},
        default="cpython-3.10",
        help="Python distribution to build",
    )
    parser.add_argument(
        "--break-on-failure",
        action="store_true",
        help="Enter a Python debugger if an error occurs",
    )
    parser.add_argument(
        "--no-docker",
        action="store_true",
        default=sys.platform == "darwin",
        help="Disable building in Docker",
    )
    parser.add_argument(
        "--serial",
        action="store_true",
        help="Build packages serially, without parallelism",
    )
    parser.add_argument(
        "--make-target",
        choices={
            "default",
            "empty",
            "toolchain",
            "toolchain-image-build",
            "toolchain-image-build.cross",
            "toolchain-image-gcc",
            "toolchain-image-xcb",
            "toolchain-image-xcb.cross",
        },
        default="default",
        help="The make target to evaluate",
    )

    args = parser.parse_args()

    target_triple = args.target_triple

    settings = get_target_settings(TARGETS_CONFIG, target_triple)

    supported_pythons = {f"cpython-{p}" for p in settings["pythons_supported"]}

    if args.python not in supported_pythons:
        print(
            f'{target_triple} only supports following Pythons: {", ".join(supported_pythons)}'
        )
        return 1

    musl = "musl" in target_triple

    env = dict(os.environ)

    env["PYBUILD_HOST_PLATFORM"] = host_platform
    env["PYBUILD_TARGET_TRIPLE"] = target_triple
    env["PYBUILD_OPTIMIZATIONS"] = args.optimizations
    if musl:
        env["PYBUILD_MUSL"] = "1"
    if args.break_on_failure:
        env["PYBUILD_BREAK_ON_FAILURE"] = "1"
    if args.no_docker:
        env["PYBUILD_NO_DOCKER"] = "1"

    entry = DOWNLOADS[args.python]
    env["PYBUILD_PYTHON_VERSION"] = entry["version"]
    env["PYBUILD_PYTHON_MAJOR_VERSION"] = ".".join(entry["version"].split(".")[:2])

    if "PYBUILD_RELEASE_TAG" in os.environ:
        release_tag = os.environ["PYBUILD_RELEASE_TAG"]
    else:
        release_tag = release_tag_from_git()

    archive_components = [
        f'cpython-{entry["version"]}',
        target_triple,
        args.optimizations,
    ]

    build_basename = "-".join(archive_components) + ".tar"
    dist_basename = "-".join(archive_components + [release_tag])

    # We run make with static parallelism no greater than the machine's CPU count
    # because we can get some speedup from parallel operations. But we also don't
    # share a make job server with each build. So if we didn't limit the
    # parallelism we could easily oversaturate the CPU. Higher levels of
    # parallelism don't result in meaningful build speedups because tk/tix has
    # a long, serial dependency chain that can't be built in parallel.
    parallelism = min(1 if args.serial else 4, multiprocessing.cpu_count())

    subprocess.run(
        ["make", "-j%d" % parallelism, args.make_target], env=env, check=True
    )

    DIST.mkdir(exist_ok=True)

    if args.make_target == "default":
        compress_python_archive(BUILD / build_basename, DIST, dist_basename)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except subprocess.CalledProcessError as e:
        sys.exit(e.returncode)
