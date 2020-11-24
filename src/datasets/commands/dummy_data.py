import json
import logging
import os
import shutil
import fnmatch
import xml.etree.ElementTree as ET
from argparse import ArgumentParser
from pathlib import Path
from typing import Optional

from datasets.commands import BaseTransformersCLICommand
from datasets.load import import_main_class, prepare_module
from datasets.utils import MockDownloadManager
from datasets.utils.download_manager import DownloadManager
from datasets.utils.file_utils import DownloadConfig
from datasets.utils.filelock import logger as filelock_logger
from datasets.utils.logging import get_logger
from datasets.utils.py_utils import map_nested


logger = get_logger(__name__)


def test_command_factory(args):
    return DummyDataCommand(
        args.path_to_dataset,
        args.auto_generate,
        args.n_lines,
        args.json_field,
        args.xml_tag,
        args.match_text_files,
        args.keep_uncompressed,
        args.cache_dir,
    )


class DummyDataGeneratorDownloadManager(DownloadManager):
    def __init__(self, mock_download_manager, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.mock_download_manager = mock_download_manager
        self.downloaded_paths = []
        self.expected_dummy_paths = []

    def download(self, url_or_urls):
        output = super().download(url_or_urls)
        dummy_output = self.mock_download_manager.download(url_or_urls)
        map_nested(self.downloaded_paths.append, output, map_tuple=True)
        map_nested(self.expected_dummy_paths.append, dummy_output, map_tuple=True)
        return output

    def download_and_extract(self, url_or_urls):
        output = super().extract(super().download(url_or_urls))
        dummy_output = self.mock_download_manager.download(url_or_urls)
        map_nested(self.downloaded_paths.append, output, map_tuple=True)
        map_nested(self.expected_dummy_paths.append, dummy_output, map_tuple=True)
        return output

    def auto_generate_dummy_data_folder(
        self, n_lines=5, json_field: Optional[str] = None, xml_tag: Optional[str] = None, match_text_files: Optional[str] = None
    ) -> bool:
        os.makedirs(
            os.path.join(
                self.mock_download_manager.datasets_scripts_dir,
                self.mock_download_manager.dataset_name,
                self.mock_download_manager.dummy_data_folder,
                "dummy_data",
            ),
            exist_ok=True,
        )
        total = 0
        for src_path, relative_dst_path in zip(self.downloaded_paths, self.expected_dummy_paths):
            dst_path = os.path.join(
                self.mock_download_manager.datasets_scripts_dir,
                self.mock_download_manager.dataset_name,
                self.mock_download_manager.dummy_data_folder,
                relative_dst_path,
            )
            total += self._create_dummy_data(
                src_path, dst_path, n_lines=n_lines, json_field=json_field, xml_tag=xml_tag, match_text_files=match_text_files
            )
        if total == 0:
            logger.error(
                "Dummy data generation failed: no dummy files were created. "
                "Make sure the data files format is supported by the auto-generation."
            )
        return total > 0

    def _create_dummy_data(
        self,
        src_path: str,
        dst_path: str,
        n_lines: int,
        json_field: Optional[str] = None,
        xml_tag: Optional[str] = None,
        match_text_files: Optional[str] = None
    ) -> int:
        if os.path.isfile(src_path):
            logger.debug(f"Trying to generate dummy data file {dst_path}")
            line_by_line_extensions = [".txt", ".csv", ".jsonl", ".tsv"]
            is_line_by_line_text_file = any(dst_path.endswith(extension) for extension in line_by_line_extensions)
            if match_text_files is not None:
                file_name = os.path.basename(dst_path)
                for pattern in match_text_files.split(","):
                    is_line_by_line_text_file |= fnmatch.fnmatch(file_name, pattern)
            if is_line_by_line_text_file:
                Path(dst_path).parent.mkdir(exist_ok=True, parents=True)
                with open(src_path, "r", encoding="utf-8") as src_file:
                    with open(dst_path, "w", encoding="utf-8") as dst_file:
                        first_lines = [next(src_file) for _ in range(n_lines)]
                        dst_file.write("".join(first_lines).strip())
                return 1
            elif dst_path.endswith(".json"):
                with open(src_path, "r", encoding="utf-8") as src_file:
                    json_data = json.load(src_file)
                    if json_field is not None:
                        json_data = json_data[json_field]
                    if isinstance(json_data, dict):
                        if not all(isinstance(v, list) for v in json_data.values()):
                            raise ValueError(
                                f"Couldn't parse columns {list(json_data.keys())}. "
                                "Maybe specify which json field must be used "
                                "to read the data with --json_field <my_field>."
                            )
                        first_json_data = {k: v[:n_lines] for k, v in json_data.items()}
                    else:
                        first_json_data = json_data[:n_lines]
                    if json_field is not None:
                        first_json_data = {json_field: first_json_data}
                    Path(dst_path).parent.mkdir(exist_ok=True, parents=True)
                    with open(dst_path, "w", encoding="utf-8") as dst_file:
                        json.dump(first_json_data, dst_file)
                return 1
            elif dst_path.endswith(".xml"):
                if xml_tag is None:
                    logger.warning("Found xml file but 'xml_tag' is set to None. Please provide --xml_tag")
                else:
                    Path(dst_path).parent.mkdir(exist_ok=True, parents=True)
                    with open(src_path, "r", encoding="utf-8") as src_file:
                        tree = ET.parse(src_file)
                        # get parent tag

                        def _get_parent_tag(tree, tag: str) -> str:
                            for element in tree.iter():
                                for subelement in element:
                                    if subelement.tag == xml_tag:
                                        return element.tag

                        parent_tag = _get_parent_tag(tree, xml_tag)
                        assert parent_tag is not None, f"couldn't find xml tag {xml_tag}"
                        # remove all the elements except the first n
                        current = 0
                        for parent in tree.iter(parent_tag):
                            children = [child for child in parent if child.tag == xml_tag]
                            first_elements = children[: n_lines - current]
                            children_to_remove = children[n_lines - current :]
                            current += len(first_elements)
                            for child_to_remove in children_to_remove:
                                parent.remove(child_to_remove)

                        tree.write(dst_path, encoding="utf-8")
                return 1
            logger.warning(
                f"Couldn't generate dummy file '{dst_path}'. "
                "Ignore that if this file is not useful for dummy data."
            )
            return 0
        elif os.path.isdir(src_path):
            total = 0
            for path, _, files in os.walk(src_path):
                for name in files:
                    if not name.startswith("."):  # ignore files like .DS_Store etc.
                        src_file_path = os.path.join(path, name)
                        dst_file_path = os.path.join(dst_path, Path(src_file_path).relative_to(src_path))
                        total += self._create_dummy_data(
                            src_file_path, dst_file_path, n_lines=n_lines, json_field=json_field, xml_tag=xml_tag, match_text_files=match_text_files
                        )
            return total

    def compress_autogenerated_dummy_data(self, path_to_dataset):
        root_dir = os.path.join(path_to_dataset, self.mock_download_manager.dummy_data_folder)
        base_name = os.path.join(root_dir, "dummy_data")
        base_dir = "dummy_data"
        logger.info(f"Compressing dummy data folder to '{base_name}.zip'")
        shutil.make_archive(base_name, "zip", root_dir, base_dir)
        shutil.rmtree(base_name)


class DummyDataCommand(BaseTransformersCLICommand):
    @staticmethod
    def register_subcommand(parser: ArgumentParser):
        test_parser = parser.add_parser("dummy_data")
        test_parser.add_argument(
            "--auto_generate", action="store_true", help="Try to automatically generate dummy data"
        )
        test_parser.add_argument(
            "--n_lines", type=int, default=5, help="number of lines used when auto-generating dummy data"
        )
        test_parser.add_argument(
            "--json_field",
            type=str,
            default=None,
            help="optional, json field to read the data from when auto-generating dummy data",
        )
        test_parser.add_argument(
            "--xml_tag",
            type=str,
            default=None,
            help="optional, xml tag name of the samples to filter when auto-generating dummy data",
        )
        test_parser.add_argument(
            "--match_text_files",
            type=str,
            default=None,
            help="optional, a regex that looks for line-by-line text files other than *.txt or *.csv for example",
        )
        test_parser.add_argument(
            "--keep_uncompressed",
            action="store_true",
            help="don't compress the dummy data folders when auto-generating dummy data",
        )
        test_parser.add_argument(
            "--cache_dir",
            type=str,
            default=None,
            help="cache directory to download and cache files when auto-generating dummy data",
        )
        test_parser.add_argument("path_to_dataset", type=str, help="Name of the dataset to download")
        test_parser.set_defaults(func=test_command_factory)

    def __init__(
        self,
        path_to_dataset: str,
        auto_generate: bool,
        n_lines: int,
        json_field: Optional[str],
        xml_tag: Optional[str],
        match_text_files: Optional[str],
        keep_uncompressed: bool,
        cache_dir: Optional[str],
    ):
        self._path_to_dataset = path_to_dataset
        if os.path.isdir(path_to_dataset):
            self._dataset_name = path_to_dataset.replace(os.sep, "/").split("/")[-1]
        else:
            self._dataset_name = path_to_dataset.replace(os.sep, "/").split("/")[-2]
        self._auto_generate = auto_generate
        self._n_lines = n_lines
        self._json_field = json_field
        self._xml_tag = xml_tag
        self._match_text_files = match_text_files
        self._keep_uncompressed = keep_uncompressed
        self._cache_dir = cache_dir

    def run(self):
        filelock_logger().setLevel(level=logging.WARNING)
        module_path, hash = prepare_module(self._path_to_dataset)
        builder_cls = import_main_class(module_path)

        # use `None` as config if no configs
        configs = builder_cls.BUILDER_CONFIGS or [None]

        for config in configs:
            if config is None:
                name = None
                version = builder_cls.VERSION
            else:
                version = config.version
                name = config.name

            dataset_builder = builder_cls(name=name, hash=hash, cache_dir=self._cache_dir)
            mock_dl_manager = MockDownloadManager(
                dataset_name=self._dataset_name,
                config=config,
                version=version,
                is_local=True,
                load_existing_dummy_data=False,
            )

            if self._auto_generate:
                self._autogenerate_dummy_data(
                    dataset_builder=dataset_builder,
                    mock_dl_manager=mock_dl_manager,
                    keep_uncompressed=self._keep_uncompressed,
                )
            else:
                self._print_dummy_data_instructions(dataset_builder=dataset_builder, mock_dl_manager=mock_dl_manager)

    def _autogenerate_dummy_data(self, dataset_builder, mock_dl_manager, keep_uncompressed):
        dl_cache_dir = os.path.join(dataset_builder._cache_dir_root, "downloads")
        download_config = DownloadConfig(cache_dir=dl_cache_dir)
        dl_manager = DummyDataGeneratorDownloadManager(
            dataset_name=self._dataset_name, mock_download_manager=mock_dl_manager, download_config=download_config
        )
        dataset_builder._split_generators(dl_manager)
        dl_manager.auto_generate_dummy_data_folder(
            n_lines=self._n_lines, json_field=self._json_field, xml_tag=self._xml_tag, match_text_files=self._match_text_files
        )
        if not keep_uncompressed:
            path_do_dataset = os.path.join(mock_dl_manager.datasets_scripts_dir, mock_dl_manager.dataset_name)
            dl_manager.compress_autogenerated_dummy_data(path_do_dataset)
        else:
            generated_dummy_data_dir = os.path.join(self._path_to_dataset, mock_dl_manager.dummy_data_folder)
            logger.info(
                f"Dummy data generated in directory '{generated_dummy_data_dir}' but kept uncompressed. "
                "Please compress this directory into a zip file to use it for dummy data tests."
            )
        return dl_manager

    def _print_dummy_data_instructions(self, dataset_builder, mock_dl_manager):
        dummy_data_folder = os.path.join(self._path_to_dataset, mock_dl_manager.dummy_data_folder)
        config = mock_dl_manager.config
        logger.info(f"Creating dummy folder structure for {dummy_data_folder}... ")
        os.makedirs(dummy_data_folder, exist_ok=True)

        try:
            generator_splits = dataset_builder._split_generators(mock_dl_manager)
        except FileNotFoundError as e:

            print(
                f"Dataset {self._dataset_name} with config {config} seems to already open files in the method `_split_generators(...)`. You might consider to instead only open files in the method `_generate_examples(...)` instead. If this is not possible the dummy data has to be created with less guidance. Make sure you create the file {e.filename}."
            )

        files_to_create = set()
        split_names = []
        dummy_file_name = mock_dl_manager.dummy_file_name

        for split in generator_splits:
            logger.info(f"Collecting dummy data file paths to create for {split.name}")
            split_names.append(split.name)
            gen_kwargs = split.gen_kwargs
            generator = dataset_builder._generate_examples(**gen_kwargs)

            try:
                dummy_data_guidance_print = "\n" + 30 * "=" + "DUMMY DATA INSTRUCTIONS" + 30 * "=" + "\n"
                config_string = f"config {config.name} of " if config is not None else ""
                dummy_data_guidance_print += (
                    "- In order to create the dummy data for "
                    + config_string
                    + f"{self._dataset_name}, please go into the folder '{dummy_data_folder}' with `cd {dummy_data_folder}` . \n\n"
                )

                # trigger generate function
                for key, record in generator:
                    pass

                dummy_data_guidance_print += f"- It appears that the function `_generate_examples(...)` expects one or more files in the folder {dummy_file_name} using the function `glob.glob(...)`. In this case, please refer to the `_generate_examples(...)` method to see under which filename the dummy data files should be created. \n\n"

            except FileNotFoundError as e:
                files_to_create.add(e.filename)

        split_names = ", ".join(split_names)
        if len(files_to_create) > 0:
            # no glob.glob(...) in `_generate_examples(...)`
            if len(files_to_create) == 1 and next(iter(files_to_create)) == dummy_file_name:
                dummy_data_guidance_print += f"- Please create a single dummy data file called '{next(iter(files_to_create))}' from the folder '{dummy_data_folder}'. Make sure that the dummy data file provides at least one example for the split(s) '{split_names}' \n\n"
                files_string = dummy_file_name
            else:
                files_string = ", ".join(files_to_create)
                dummy_data_guidance_print += f"- Please create the following dummy data files '{files_string}' from the folder '{dummy_data_folder}'\n\n"

                dummy_data_guidance_print += f"- For each of the splits '{split_names}', make sure that one or more of the dummy data files provide at least one example \n\n"

            dummy_data_guidance_print += f"- If the method `_generate_examples(...)` includes multiple `open()` statements, you might have to create other files in addition to '{files_string}'. In this case please refer to the `_generate_examples(...)` method \n\n"

        if len(files_to_create) == 1 and next(iter(files_to_create)) == dummy_file_name:
            dummy_data_guidance_print += f"-After the dummy data file is created, it should be zipped to '{dummy_file_name}.zip' with the command `zip {dummy_file_name}.zip {dummy_file_name}` \n\n"

            dummy_data_guidance_print += (
                f"-You can now delete the file '{dummy_file_name}' with the command `rm {dummy_file_name}` \n\n"
            )

            dummy_data_guidance_print += f"- To get the file '{dummy_file_name}' back for further changes to the dummy data, simply unzip {dummy_file_name}.zip with the command `unzip {dummy_file_name}.zip` \n\n"
        else:
            dummy_data_guidance_print += f"-After all dummy data files are created, they should be zipped recursively to '{dummy_file_name}.zip' with the command `zip -r {dummy_file_name}.zip {dummy_file_name}/` \n\n"

            dummy_data_guidance_print += (
                f"-You can now delete the folder '{dummy_file_name}' with the command `rm -r {dummy_file_name}` \n\n"
            )

            dummy_data_guidance_print += f"- To get the folder '{dummy_file_name}' back for further changes to the dummy data, simply unzip {dummy_file_name}.zip with the command `unzip {dummy_file_name}.zip` \n\n"

        dummy_data_guidance_print += (
            f"- Make sure you have created the file '{dummy_file_name}.zip' in '{dummy_data_folder}' \n"
        )

        dummy_data_guidance_print += 83 * "=" + "\n"

        print(dummy_data_guidance_print)
