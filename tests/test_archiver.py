import io
import runpy
import tempfile
import types
import unittest
import zipfile
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MODULE = runpy.run_path(str(ROOT / "3dsMax Archive.py"), run_name="archiver_under_test")
PathExtractor = MODULE["PathExtractor"]
MaxParser = MODULE["MaxParser"]
Archiver = MODULE["Archiver"]
App = MODULE["App"]


class PathExtractorTests(unittest.TestCase):
    def test_extracts_relative_and_unc_paths(self):
        data = (
            b"\0C:\\assets\\floor.exr\0"
            + b"maps\\wall.jpg\0"
            + b"..\\ies\\studio.ies\0"
            + b"\\\\server\\assets\\proxies\\tree.vrmesh\0"
            + b"\0"
            + "textures\\пол.png".encode("utf-16-le")
            + b"\0\0"
        )

        paths = PathExtractor().extract_paths(data)

        self.assertEqual(
            paths,
            {
                r"C:\assets\floor.exr",
                r"maps\wall.jpg",
                r"..\ies\studio.ies",
                r"\\server\assets\proxies\tree.vrmesh",
                "textures\\пол.png",
            },
        )

    def test_resolves_relative_path_against_scene_directory(self):
        extractor = PathExtractor()
        scene_dir = r"D:\projects\scene"

        self.assertEqual(
            extractor.resolve(r"maps\wall.jpg", scene_dir),
            r"D:\projects\scene\maps\wall.jpg",
        )
        self.assertEqual(
            extractor.resolve(r"\\server\share\wall.jpg", scene_dir),
            r"\\server\share\wall.jpg",
        )


class AppPathTests(unittest.TestCase):
    def test_default_archive_path_follows_selected_scene(self):
        self.assertEqual(
            App.default_archive_path(r"D:\project\Scene_01.max"),
            r"D:\project\Scene_01_archive.zip",
        )
        self.assertEqual(
            App.default_archive_path("D:/project/Scene_02.MAX"),
            "D:/project/Scene_02_archive.zip",
        )
        self.assertEqual(App.default_archive_path(""), "")


class RelativeArchiveTests(unittest.TestCase):
    def setUp(self):
        self.parser_globals = MaxParser.parse.__globals__
        self.previous_has_olefile = self.parser_globals["HAS_OLEFILE"]
        self.parser_globals["HAS_OLEFILE"] = False

    def tearDown(self):
        self.parser_globals["HAS_OLEFILE"] = self.previous_has_olefile

    def test_relative_asset_is_added_to_archive(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            maps = root / "maps"
            maps.mkdir()
            texture = maps / "wall.jpg"
            texture.write_bytes(b"texture")

            scene = root / "scene.max"
            scene.write_bytes(b"\0maps\\wall.jpg\0")
            archive = root / "scene.zip"

            archiver = Archiver.__new__(Archiver)
            archiver.log_func = None
            archiver.parser = MaxParser()

            ok, _, _, stats = archiver.create(
                str(scene), str(archive), {"texture": True}, organize=True
            )

            self.assertTrue(ok)
            self.assertEqual(stats["resources"], 1)
            self.assertEqual(stats["missing"], 0)
            self.assertEqual(stats["errors"], [])
            with zipfile.ZipFile(archive) as zip_file:
                self.assertIn("maps/wall.jpg", zip_file.namelist())

    def test_missing_asset_is_found_near_scene_by_filename(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            textures = root / "textures"
            textures.mkdir()
            texture = textures / "wall.jpg"
            texture.write_bytes(b"texture")

            scene = root / "scene.max"
            scene.write_bytes(b"\0missing\\wall.jpg\0")
            archive = root / "scene.zip"

            archiver = Archiver.__new__(Archiver)
            archiver.log_func = None
            archiver.parser = MaxParser()

            ok, _, _, stats = archiver.create(
                str(scene), str(archive), {"texture": True}, organize=True
            )

            self.assertTrue(ok)
            self.assertEqual(stats["resources"], 1)
            self.assertEqual(stats["missing"], 0)
            self.assertEqual(stats["errors"], [])
            with zipfile.ZipFile(archive) as zip_file:
                self.assertIn("maps/wall.jpg", zip_file.namelist())

    def test_near_scene_search_prefers_matching_path_suffix(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            assets = root / "assets"
            other = root / "other"
            assets.mkdir()
            other.mkdir()
            preferred = assets / "same.jpg"
            preferred.write_bytes(b"preferred")
            (other / "same.jpg").write_bytes(b"other")

            scene = root / "scene.max"
            scene.write_bytes(b"\0offline\\assets\\same.jpg\0")
            archive = root / "scene.zip"

            archiver = Archiver.__new__(Archiver)
            archiver.log_func = None
            archiver.parser = MaxParser()

            ok, _, _, stats = archiver.create(
                str(scene), str(archive), {"texture": True}, organize=True
            )

            self.assertTrue(ok)
            self.assertEqual(stats["resources"], 1)
            self.assertEqual(stats["missing"], 0)
            with zipfile.ZipFile(archive) as zip_file:
                self.assertEqual(zip_file.read("maps/same.jpg"), b"preferred")

    def test_duplicate_filenames_are_placed_in_separate_folders(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            relative_paths = []

            for folder, content in (
                ("one", b"first"),
                ("two", b"second"),
                ("three", b"third"),
            ):
                asset_dir = root / folder
                asset_dir.mkdir()
                (asset_dir / "same.jpg").write_bytes(content)
                relative_paths.append(f"{folder}\\same.jpg")

            scene = root / "scene.max"
            scene.write_bytes(
                b"\0" + b"\0".join(path.encode("ascii") for path in relative_paths) + b"\0"
            )
            archive = root / "scene.zip"

            archiver = Archiver.__new__(Archiver)
            archiver.log_func = None
            archiver.parser = MaxParser()

            ok, _, _, stats = archiver.create(
                str(scene), str(archive), {"texture": True}, organize=True
            )

            self.assertTrue(ok)
            self.assertEqual(stats["resources"], 3)
            self.assertEqual(stats["missing"], 0)
            self.assertEqual(stats["errors"], [])
            with zipfile.ZipFile(archive) as zip_file:
                names = set(zip_file.namelist())

            self.assertIn("maps/same.jpg", names)
            self.assertIn("maps/duplicates_1/same.jpg", names)
            self.assertIn("maps/duplicates_2/same.jpg", names)
            self.assertFalse(any("same_" in name for name in names))

    def test_missing_resource_is_returned_as_warning(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            scene = root / "scene.max"
            scene.write_bytes(b"\0maps\\missing.jpg\0")
            archive = root / "scene.zip"

            archiver = Archiver.__new__(Archiver)
            archiver.log_func = None
            archiver.parser = MaxParser()

            ok, _, _, stats = archiver.create(
                str(scene), str(archive), {"texture": True}, organize=True
            )

            self.assertTrue(ok)
            self.assertEqual(stats["missing"], 1)
            self.assertEqual(stats["errors"], [])
            with zipfile.ZipFile(archive) as zip_file:
                report = zip_file.read("_report.txt").decode("utf-8")
            self.assertIn("ОТСУТСТВУЕТ (1)", report)
            self.assertIn(str(root / "maps" / "missing.jpg"), report)
            self.assertIn("Ресурсов добавлено: 0", report)

    def test_missing_resource_in_unselected_category_is_not_a_warning(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            scene = root / "scene.max"
            scene.write_bytes(b"\0maps\\missing.jpg\0")
            archive = root / "scene.zip"

            archiver = Archiver.__new__(Archiver)
            archiver.log_func = None
            archiver.parser = MaxParser()

            ok, _, _, stats = archiver.create(
                str(scene), str(archive), {"texture": False}, organize=True
            )

            self.assertTrue(ok)
            self.assertEqual(stats["missing"], 0)
            self.assertEqual(stats["errors"], [])
            with zipfile.ZipFile(archive) as zip_file:
                report = zip_file.read("_report.txt").decode("utf-8")
            self.assertNotIn("ОТСУТСТВУЕТ", report)

    def test_resource_write_error_is_returned_as_warning(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            texture = root / "broken.jpg"
            texture.write_bytes(b"texture")
            scene = root / "scene.max"
            scene.write_bytes(b"\0broken.jpg\0")
            archive = root / "scene.zip"

            archiver = Archiver.__new__(Archiver)
            archiver.log_func = None
            archiver.parser = MaxParser()
            original_write = zipfile.ZipFile.write

            def fail_for_texture(zip_file, filename, *args, **kwargs):
                if Path(filename) == texture:
                    raise OSError("test write error")
                return original_write(zip_file, filename, *args, **kwargs)

            with mock.patch.object(zipfile.ZipFile, "write", new=fail_for_texture):
                ok, _, _, stats = archiver.create(
                    str(scene), str(archive), {"texture": True}, organize=True
                )

            self.assertTrue(ok)
            self.assertEqual(stats["missing"], 0)
            self.assertEqual(len(stats["errors"]), 1)
            with zipfile.ZipFile(archive) as zip_file:
                report = zip_file.read("_report.txt").decode("utf-8")
            self.assertIn("ОШИБКИ ДОБАВЛЕНИЯ (1)", report)
            self.assertIn("test write error", report)

    def test_ole_parser_uses_only_asset_metadata_stream(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            maps = root / "maps"
            maps.mkdir()
            texture = maps / "wall.jpg"
            texture.write_bytes(b"texture")
            scene = root / "scene.max"
            scene.write_bytes(b"fake OLE scene")

            summary_stream = ["\x05DocumentSummaryInformation"]
            metadata_stream = ["FileAssetMetaData3"]
            scene_stream = ["Scene_Compressed"]
            stream_data = {
                tuple(summary_stream): b"\0ghost.jpg\0",
                tuple(metadata_stream): b"\0maps\\wall.jpg\0",
                tuple(scene_stream): b"\0.40.rs\0",
            }

            fake_ole = mock.Mock()
            fake_ole.listdir.return_value = [
                summary_stream, metadata_stream, scene_stream
            ]
            fake_ole.openstream.side_effect = (
                lambda stream: io.BytesIO(stream_data[tuple(stream)])
            )
            fake_ole_module = types.SimpleNamespace(
                OleFileIO=lambda _filepath: fake_ole
            )

            with mock.patch.dict(
                self.parser_globals,
                {"HAS_OLEFILE": True, "olefile": fake_ole_module},
            ):
                result = MaxParser().parse(str(scene))

            self.assertEqual(result, {"texture": {str(texture)}})
            fake_ole.openstream.assert_called_once_with(metadata_stream)
            fake_ole.close.assert_called_once()

    def test_xref_scene_and_its_resources_are_archived_recursively(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            xref_dir = root / "xrefs"
            asset_dir = xref_dir / "assets"
            asset_dir.mkdir(parents=True)

            texture = asset_dir / "xref_texture.jpg"
            texture.write_bytes(b"xref texture")

            nested_scene = xref_dir / "nested.max"
            nested_scene.write_bytes(
                b"\0assets\\xref_texture.jpg\0..\\scene.max\0"
            )

            scene = root / "scene.max"
            scene.write_bytes(b"\0xrefs\\nested.max\0")
            archive = root / "scene.zip"

            archiver = Archiver.__new__(Archiver)
            archiver.log_func = None
            archiver.parser = MaxParser()

            ok, _, _, stats = archiver.create(
                str(scene),
                str(archive),
                {"texture": True, "xref": True},
                organize=True,
            )

            self.assertTrue(ok)
            self.assertEqual(stats["resources"], 2)
            self.assertEqual(stats["missing"], 0)
            self.assertEqual(stats["errors"], [])
            with zipfile.ZipFile(archive) as zip_file:
                names = set(zip_file.namelist())

            self.assertIn("scene.max", names)
            self.assertIn("xrefs/nested.max", names)
            self.assertIn("maps/xref_texture.jpg", names)
            self.assertNotIn("xrefs/scene.max", names)

    def test_missing_xref_is_found_near_scene_and_parsed_recursively(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            xref_dir = root / "xrefs"
            maps_dir = xref_dir / "maps"
            maps_dir.mkdir(parents=True)

            texture = maps_dir / "xref_texture.jpg"
            texture.write_bytes(b"xref texture")

            nested_scene = xref_dir / "nested.max"
            nested_scene.write_bytes(b"\0maps\\xref_texture.jpg\0")

            scene = root / "scene.max"
            scene.write_bytes(b"\0missing\\nested.max\0")
            archive = root / "scene.zip"

            archiver = Archiver.__new__(Archiver)
            archiver.log_func = None
            archiver.parser = MaxParser()

            ok, _, _, stats = archiver.create(
                str(scene),
                str(archive),
                {"texture": True, "xref": True},
                organize=True,
            )

            self.assertTrue(ok)
            self.assertEqual(stats["resources"], 2)
            self.assertEqual(stats["missing"], 0)
            self.assertEqual(stats["errors"], [])
            with zipfile.ZipFile(archive) as zip_file:
                names = set(zip_file.namelist())

            self.assertIn("xrefs/nested.max", names)
            self.assertIn("maps/xref_texture.jpg", names)


if __name__ == "__main__":
    unittest.main()
