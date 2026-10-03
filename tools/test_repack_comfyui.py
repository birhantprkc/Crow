"""repack_comfyui.py on a small 7z built here with bsdtar (#340)."""

import io
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import repack_comfyui as R  # noqa: E402

FILES = {
    "ComfyUI_windows_portable/run_nvidia_gpu.bat": b".\\python_embeded\\python.exe -s ComfyUI\\main.py\r\n",
    "ComfyUI_windows_portable/ComfyUI/main.py": b"print('main')\n" * 50,
    "ComfyUI_windows_portable/python_embeded/python.exe": bytes(range(256)) * 64,
    "ComfyUI_windows_portable/ComfyUI/models/vae/put_vae_here": b"",
}


@unittest.skipIf(R.bsdtar() is None, "bsdtar is not on this machine")
class Repack(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        src = os.path.join(self.tmp.name, "src")
        for name, data in FILES.items():
            path = os.path.join(src, *name.split("/"))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(data)
        self.archive = os.path.join(self.tmp.name, "portable.7z")
        subprocess.run([R.bsdtar(), "--format", "7zip", "-cf", self.archive, "-C", src,
                        "ComfyUI_windows_portable"], check=True)

    def repack(self, out, *extra):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = R.main([self.archive, out] + list(extra))
        return code, buf.getvalue()

    def test_the_zip_holds_every_file_sorted_with_fixed_times(self):
        out = os.path.join(self.tmp.name, "a.zip")
        code, text = self.repack(out, "--sha256", R.sha256_of(self.archive))
        self.assertEqual(code, 0, text)
        with zipfile.ZipFile(out) as z:
            infos = z.infolist()
            self.assertEqual([i.filename for i in infos], sorted(FILES))
            for i in infos:
                self.assertEqual(i.date_time, R.EPOCH)
                self.assertEqual(z.read(i), FILES[i.filename])
        self.assertIn("files 4", text)
        self.assertIn("sha256 %s" % R.sha256_of(out), text)
        self.assertFalse(os.path.exists(out + ".part"))

    def test_the_same_input_gives_the_same_bytes(self):
        a, b = os.path.join(self.tmp.name, "a.zip"), os.path.join(self.tmp.name, "b.zip")
        self.assertEqual(self.repack(a)[0], 0)
        self.assertEqual(self.repack(b)[0], 0)
        self.assertEqual(R.sha256_of(a), R.sha256_of(b))

    def test_a_wrong_digest_refuses_and_writes_nothing(self):
        out = os.path.join(self.tmp.name, "a.zip")
        code, text = self.repack(out, "--sha256", "0" * 64)
        self.assertEqual(code, 1)
        self.assertIn("FAIL input sha256", text)
        self.assertFalse(os.path.exists(out))

    def test_verify_finds_a_changed_a_missing_and_an_extra_file(self):
        out = os.path.join(self.tmp.name, "a.zip")
        self.assertEqual(self.repack(out)[0], 0)
        tree = os.path.join(self.tmp.name, "tree")
        with zipfile.ZipFile(out) as z:
            z.extractall(tree)
        files = R.tree(tree)
        with open(files["ComfyUI_windows_portable/ComfyUI/main.py"], "ab") as f:
            f.write(b"#")
        del files["ComfyUI_windows_portable/run_nvidia_gpu.bat"]
        extra = os.path.join(tree, "new.txt")
        open(extra, "wb").close()
        files["new.txt"] = extra
        problems = R.verify(files, out)
        self.assertIn("zip entry ComfyUI_windows_portable/ComfyUI/main.py differs from the extracted file",
                      problems)
        self.assertIn("zip has ComfyUI_windows_portable/run_nvidia_gpu.bat, which the archive does not",
                      problems)
        self.assertIn("zip lacks new.txt", problems)

    def test_a_missing_archive_is_a_setup_error(self):
        self.assertEqual(R.main([os.path.join(self.tmp.name, "none.7z"), "x.zip"]), 2)


if __name__ == "__main__":
    unittest.main()
