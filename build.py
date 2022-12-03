import os
import shutil
import subprocess
from zipfile import ZipFile, ZIP_LZMA

import venv

import constants


def create_env(path: str):
    venv.create(path, clear=True, with_pip=True)


def pip_install_requirements(env_path: str):
    subprocess.check_call([os.path.join(env_path, python + ext), "-m", "pip", "install", "-r", "requirements.txt"])


def pyarmor(script_path: str, name: str):
    subprocess.run(f'{os.path.join(script_path, "pyarmor")} pack --clean --name={name} '
                   f'-e " --onefile --icon=app.ico --noupx" '
                   f'-x " --exclude venv '
                   f'--exclude modules '
                   f'--exclude build.py '
                   f'--advanced 1" main.py')


def get_all_file_paths(directory):
    file_paths = []
    for root, directories, files in os.walk(directory):
        for filename in files:
            filepath = os.path.join(root, filename)
            file_paths.append(filepath)
    return file_paths


def zip_build(source: str, file: str, zip_name: str):
    with ZipFile(f'{zip_name}.zip', 'w', compression=ZIP_LZMA) as z:
        z.write(os.path.join(source, file), arcname=file)


def cleanup(d: str):
    shutil.rmtree(d)


if os.name == 'nt':
    ext = '.exe'
    python = 'python'
else:
    ext = ''
    python = 'python3'

working_dir = os.getcwd()
env = os.path.join(working_dir, "venv")
scripts = os.path.join(env, "Scripts")

if os.path.exists(os.path.join(working_dir, 'dist')):
    cleanup(os.path.join(working_dir, 'dist'))
create_env(env)
pip_install_requirements(scripts)
pyarmor(scripts, f'{constants.name}')
if os.path.exists(os.path.join(working_dir, 'build')):
    cleanup(os.path.join(working_dir, 'build'))
source = os.path.join(working_dir, 'dist')
dest = os.path.join(working_dir, 'dist', f'{constants.name}-{constants.version}-{constants.os}')
file = os.listdir(source)
if len(file) > 0:
    zip_build(source, file[0], dest)
