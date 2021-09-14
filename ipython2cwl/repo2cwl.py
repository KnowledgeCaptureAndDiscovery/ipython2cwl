import argparse
import logging
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path
from typing import List, Optional, Tuple, Dict
from urllib.parse import urlparse, ParseResult

import git  # type: ignore
import nbformat  # type: ignore
import yaml
from git import Repo
from repo2docker import Repo2Docker  # type: ignore

from .cwltoolextractor import AnnotatedIPython2CWLToolConverter

logger = logging.getLogger('repo2cwl')
logger.setLevel(logging.INFO)


def _get_notebook_paths_from_dir(dir_path: str):
    notebooks_paths = []
    for path, dirs, files in os.walk(dir_path):
        dirs[:] = [d for d in dirs if d not in "cwl"]
        for name in files:
            if name.endswith('.ipynb'):
                notebooks_paths.append(os.path.join(path, name))
    return notebooks_paths


def _store_jn_as_script(notebook_path: str, git_directory_absolute_path: str, bin_absolute_path: str, image_id: str) \
        -> Tuple[Optional[Dict], Optional[str]]:
    with open(notebook_path) as fd:
        notebook = nbformat.read(fd, as_version=4)

    converter = AnnotatedIPython2CWLToolConverter.from_jupyter_notebook_node(notebook)

    if len(converter._variables) == 0:
        logger.info(f"Notebook {notebook_path} does not contains typing annotations. skipping...")
        return None, None

    #change the extension from ipynb to nothing
    notebook_absolute = Path(notebook_path)
    notebook_relative_to_git = notebook_absolute.relative_to(git_directory_absolute_path)
    notebook_name_without_extension = notebook_absolute.stem
    script_absolute = Path(bin_absolute_path) / notebook_relative_to_git.parent / notebook_name_without_extension
    #write the script, return it relative to
    script_relative_to_git = script_absolute.relative_to(bin_absolute_path)
    script = os.linesep.join([
        '#!/usr/bin/env ipython',
        '"""',
        'DO NOT EDIT THIS FILE',
        'THIS FILE IS AUTO-GENERATED BY THE ipython2cwl.',
        'FOR MORE INFORMATION CHECK https://github.com/giannisdoukas/ipython2cwl',
        '"""\n\n',
        converter._wrap_script_to_method(converter._tree, converter._variables)
    ])
    with open(script_absolute, 'w') as fd:
        fd.write(script)
    tool = converter.cwl_command_line_tool(image_id)
    tool_st = os.stat(script_absolute)
    os.chmod(script_absolute, tool_st.st_mode | stat.S_IEXEC)
    return tool, str(script_relative_to_git)


def existing_path(path_str: str):
    path: Path = Path(path_str)
    if not path.is_dir():
        raise argparse.ArgumentTypeError(f'Directory: {str(path)} does not exists')
    return path


def parser_arguments(argv: List[str]):
    parser = argparse.ArgumentParser()
    parser.add_argument('repo', type=lambda uri: urlparse(uri, scheme='file'), nargs=1)
    parser.add_argument('-o', '--output', help='Output directory to store the generated cwl files',
                        type=existing_path,
                        required=True)

    parser.add_argument('-l', '--log_file', help='Log file',
                        type=str,
                        required=True)
    return parser.parse_args(argv)


def setup_logger(log_file):
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    fh = logging.FileHandler(log_file)
    fh.setFormatter(formatter)
    logger.addHandler(fh)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    logger.addHandler(handler)

def repo2cwl(argv: Optional[List[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    args = parser_arguments(argv)
    log_file: str = args.log_file
    setup_logger(log_file)
    uri: ParseResult = args.repo[0]
    if uri.path.startswith('git@') and uri.path.endswith('.git'):
        uri = urlparse(f'ssh://{uri.path}')
    output_directory: Path = args.output
    supported_schemes = {'file', 'http', 'https', 'ssh'}
    if uri.scheme not in supported_schemes:
        raise ValueError(f'Supported schema uris: {supported_schemes}')
    local_git_directory = os.path.join(tempfile.mkdtemp(prefix='repo2cwl_'), 'repo')
    if uri.scheme == 'file':
        if not os.path.isdir(uri.path):
            raise ValueError(f'Directory does not exists')
        logger.info(f'copy repo to temp directory: {local_git_directory}')
        shutil.copytree(uri.path, local_git_directory)
        try:
            local_git = git.Repo(local_git_directory)
        except git.InvalidGitRepositoryError:
            local_git = git.Repo.init(local_git_directory)
            local_git.git.add(A=True)
            local_git.index.commit("initial commit")
    elif uri.scheme == 'ssh':
        url = uri.geturl()[6:]
        logger.info(f'cloning repo {url} to temp directory: {local_git_directory}')
        local_git = git.Repo.clone_from(url, local_git_directory)
    else:
        logger.info(f'cloning repo to temp directory: {local_git_directory}')
        local_git = git.Repo.clone_from(uri.geturl(), local_git_directory)

    image_id, cwl_tools = _repo2cwl(local_git)
    logger.info(f'Generated image id: {image_id}')
    for tool in cwl_tools:
        script_name_path = Path(tool["baseCommand"]).stem
        base_command_script_name = f"""{str(script_name_path)}.cwl"""
        tool_filename = str(output_directory.joinpath(base_command_script_name))
        with open(tool_filename, 'w') as f:
            logger.info(f'Creating CWL command line tool: {tool_filename}')
            yaml.safe_dump(tool, f)

    logger.info(f'Cleaning local temporary directory {local_git_directory}...')
    shutil.rmtree(local_git_directory)
    return 0


def _repo2cwl(git_directory_path: Repo) -> Tuple[str, List[Dict]]:
    """
    Takes a Repo mounted to a local directory. That function will create new files and it will commit the changes.
    Do not use that function for Repositories you do not want to change them.
    :param git_directory_path:
    :return: The generated build image id & the cwl description
    """
    r2d = Repo2Docker()
    r2d.target_repo_dir = os.path.join(os.path.sep, 'app')
    r2d.repo = git_directory_path.tree().abspath
    bin_path = os.path.join(r2d.repo, 'cwl', 'bin')
    shutil.copytree(r2d.repo, bin_path)

    notebooks_paths = _get_notebook_paths_from_dir(r2d.repo)
    logger.info(notebooks_paths)
    
    tools = []
    for notebook in notebooks_paths:
        cwl_command_line_tool, script_name = _store_jn_as_script(
            notebook,
            git_directory_path.tree().abspath,
            bin_path,
            r2d.output_image_spec
        )
        if cwl_command_line_tool is None or script_name is None:
            continue
        cwl_command_line_tool['baseCommand'] = os.path.join('/app', 'cwl', 'bin', script_name)
        tools.append(cwl_command_line_tool)
    git_directory_path.index.commit("auto-commit")
    r2d.log.addHandler(logger.handlers[0])
    r2d.build()
    # fix dockerImageId
    for cwl_command_line_tool in tools:
        cwl_command_line_tool['hints']['DockerRequirement']['dockerImageId'] = r2d.output_image_spec
    return r2d.output_image_spec, tools


if __name__ == '__main__':
    repo2cwl()
    