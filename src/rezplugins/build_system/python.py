"""
Built-in simple python build system
"""
import os
import glob
import shutil
import subprocess
import sys
import functools
import argparse
from pipes import quote

from rez.build_system import BuildSystem
from rez.build_process_ import BuildType
from rez.util import create_forwarding_script
from rez.packages_ import get_developer_package
from rez.resolved_context import ResolvedContext
from rez.config import config
from rez.utils.colorize import heading, Printer


class PythonBuildSystem(BuildSystem):
    """The standard python setup.py build system.

    This is a wrapper around the normal python setup.py build system.  It
    allows python projects to seamlessly build using rez without having to
    create a cmake or bez build wrapper.

    """

    def __init__(self,
                 working_dir,
                 opts=None,
                 package=None,
                 write_build_scripts=False,
                 verbose=False,
                 build_args=None,
                 child_build_args=None):
        super(PythonBuildSystem, self).__init__(
            working_dir,
            opts=opts,
            package=package,
            write_build_scripts=write_build_scripts,
            verbose=verbose,
            build_args=build_args or [],
            child_build_args=child_build_args or [],
        )

    @classmethod
    def name(cls):
        """Return the name of the build system"""
        return "python"

    @classmethod
    def is_valid_root(cls, path, package=None):
        """Return True if this build system can build the source in path.

        For backwards compatibility with previously existing build systems
        that may have already been used to build python projects, this
        plugin will not attempt to build python projects that are setup
        for CMake or bez.
        """
        exclude = ['rezbuild.py', 'CMakeLists.txt']
        return (
            os.path.isfile(os.path.join(path, 'setup.py'))
            and not any(os.path.isfile(os.path.join(path, fn)) for fn in exclude)
        )

    def build(self,
              context,
              variant,
              build_path,
              install_path,
              install=False,
              build_type=BuildType.local,
              ):
        """Implement this method to perform the actual build.

        Args:
            context: A ResolvedContext object that the build process must be
                executed within.
            variant (`Variant`): The variant being built.
            build_path: Where to write temporary build files. May be relative
                to working_dir.
            install_path (str): The package repository path to install the
                package to, if installing. If None, defaults to
                `config.local_packages_path`.
            install: If True, install the build.
            build_type: A BuildType (i.e local or central).

        Returns:
            A dict containing the following information:
            - success: Bool indicating if the build was successful.
            - extra_files: List of created files of interest, not including
                build targets. A good example is the interpreted context file,
                usually named 'build.rxt.sh' or similar. These files should be
                located under build_path. Rez may install them for debugging
                purposes.
            - build_env_script: If this instance was created with write_build_scripts
                as True, then the build should generate a script which, when run
                by the user, places them in the build environment.

        Examples:
            # Normal build and install
            rez-build -i

            # Build and install in setuptools develop mode
            rez-build -i -- develop

            # Build with pip
            rez-build -i -- pip

        """

        ret = {}
        if self.write_build_scripts:
            # write out the script that places the user in a build env, where
            # they can run bez directly themselves.
            build_env_script = os.path.join(build_path, "build-env")
            create_forwarding_script(
                build_env_script,
                module=("build_system", self.name),
                func_name="_FWD__spawn_build_shell",
                working_dir=self.working_dir,
                build_path=build_path,
                variant_index=variant.index,
                install=install,
                install_path=install_path,
            )
            ret["success"] = True
            ret["build_env_script"] = build_env_script
            return ret

        # Creates the action callback to setup the build environment
        # after the build context is resolved.
        def _make_callack(env=None):
            def _setup_build_environment(executor):
                self.set_standard_vars(
                    executor=executor,
                    context=context,
                    variant=variant,
                    build_type=build_type,
                    install=install,
                    build_path=build_path,
                    install_path=install_path,
                )
                if env:
                    for key, value in env.items():
                        if isinstance(value, (list, tuple)):
                            for path_ in value:
                                executor.env[key].append(path_)
                        else:
                            executor.env[key] = value
            return _setup_build_environment

        build_args = self.build_args or []
        install_mode = install
        develop_mode = 'develop' in build_args
        pip_mode = 'pip' in build_args

        source_path = self.working_dir
        py_install_root = os.path.join(source_path, 'build', '_py_install')
        py_develop_root = os.path.join(source_path, 'build', '_py_develop')
        dist_root = os.path.join(source_path, 'dist')
        setup_py = os.path.join(source_path, 'setup.py')
        prefix = 'rez'

        def _run_context_shell(command, cwd, env=None):
            """Run a command within a resolved build context.
            """
            _callback = _make_callack(env)
            retcode, _, _ = context.execute_shell(
                command=command,
                block=True,
                cwd=cwd,
                parent_environ=None,
                actions_callback=_callback,
            )
            return retcode

        def _copy_tree(src, dest):
            # Utility function to copy tree.
            if os.path.exists(dest):
                shutil.rmtree(dest)
            if os.path.exists(src):
                shutil.copytree(src, dest)

        def _setup_py_build():
            """Performs a normal setup.py build."""
            cmds = ['python', setup_py, 'install', '--root', py_install_root,
                    '--prefix', prefix, '-f']
            return _run_context_shell(cmds, cwd=source_path)

        def _pip_build():
            """Performs a pip build."""
            #TODO: pip 10 will install wheel builds by default, making this
            # intermediate wheel build step unnecessary.
            print('Building wheel...')
            pip = 'pip'
            # These args speed up pip builds
            pip_args = ['--disable-pip-version-check', '--no-deps', '--no-index']
            cmds = [pip, 'wheel', '.', '-w', dist_root] + pip_args
            _run_context_shell(cmds, cwd=source_path)

            # Install wheel that we just built
            print('Installing wheel...')
            wheel_path = glob.glob(os.path.join(dist_root, '*.whl'))[0]
            cmds = [pip, 'install', wheel_path, '--root', py_install_root, '--prefix', prefix] + pip_args
            # subprocess.call(cmds, cwd=source_path)
            return _run_context_shell(cmds, cwd=source_path)

        def _build():
            """Normal setup.py build mode."""
            # Clear out old python builds
            if os.path.exists(py_install_root):
                shutil.rmtree(py_install_root)
            if os.path.isdir(dist_root):
                shutil.rmtree(dist_root)

            if pip_mode:
                retcode = _pip_build()
            else:
                retcode = _setup_py_build()

            # Move/Copy build files into rez-build directory
            _copy_tree(
                os.path.join(py_install_root, prefix, 'Lib', 'site-packages'),
                os.path.join(build_path, 'python')
            )
            _copy_tree(
                os.path.join(py_install_root, prefix, 'Scripts'),
                os.path.join(build_path, 'bin')
            )
            return retcode

        def _develop():
            """setup.py build in develop mode"""
            # Clear out old develop builds
            if os.path.exists(py_develop_root):
                shutil.rmtree(py_develop_root)

            # Create develop locations, required by setuptools
            python_dir = os.path.join(py_develop_root, 'python')
            bin_dir = os.path.join(py_develop_root, 'bin')
            os.makedirs(python_dir)
            os.makedirs(bin_dir)

            # Temporarily add develop install location to PYTHONPATH, otherwise
            # setuptools will refuse to create .pth files inside the develop python directory.
            env = {'PYTHONPATH': [python_dir]}
            # Run normal python setup develop command
            cmds = ['python', setup_py, 'develop', '-d', python_dir, '-s', bin_dir]
            retcode = _run_context_shell(cmds, cwd=source_path, env=env)

            # Move/Copy develop files into rez-build directory
            _copy_tree(
                os.path.join(py_develop_root, 'python'),
                os.path.join(build_path, 'python')
            )
            _copy_tree(
                os.path.join(py_develop_root, 'bin'),
                os.path.join(build_path, 'bin')
            )
            return retcode

        def _install():
            # Copy build to install location
            for name in ['python', 'bin']:
                _copy_tree(
                    src=os.path.join(build_path, name),
                    dest=os.path.join(install_path, name)
                )

        if self.verbose:
            pr = Printer(sys.stdout)
            pr('Running setup.py build...')

        if develop_mode:
            retcode = _develop()
        else:
            retcode = _build()

        if install_mode:
            _install()

        ret["success"] = (not retcode)
        return ret


def _FWD__spawn_build_shell(working_dir, build_path, variant_index, install,
                            install_path=None):
    # This spawns a shell that the user can run 'bez' in directly
    context = ResolvedContext.load(os.path.join(build_path, "build.rxt"))
    package = get_developer_package(working_dir)
    variant = package.get_variant(variant_index)
    config.override("prompt", "BUILD>")
    callback = functools.partial(PythonBuildSystem.set_standard_vars,
                                 context=context,
                                 package=package,
                                 variant=variant,
                                 build_type=BuildType.local,
                                 install=install,
                                 build_path=build_path,
                                 install_path=install_path)

    retcode, _, _ = context.execute_shell(block=True, cwd=build_path,
                                          actions_callback=callback)
    sys.exit(retcode)


def register_plugin():
    return PythonBuildSystem


# Copyright 2018 Brendan Abel
# Copyright 2013-2016 Allan Johns.
#
# This library is free software: you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation, either
# version 3 of the License, or (at your option) any later version.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library.  If not, see <http://www.gnu.org/licenses/>.
