#!/usr/bin/env python3
import hashlib
import json
import tarfile
import tempfile
from pathlib import Path


def main():
    target_dir = Path("demo/fixtures/editor")
    target_dir.mkdir(parents=True, exist_ok=True)

    bundles = [
        {"name": "bundle_1", "duration": 2.0, "title": "Shot 1"},
        {"name": "bundle_2", "duration": 3.0, "title": "Shot 2"},
    ]

    for b in bundles:
        bundle_name = b["name"]
        tar_path = target_dir / f"{bundle_name}.tar.gz"
        readme_path = target_dir / f"{bundle_name}_README.txt"

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            
            # Create fake mp4
            mp4_path = tmp / "out.mp4"
            mp4_path.write_bytes(b"fake video content " + bundle_name.encode("utf-8"))
            
            # Create fake composition
            comp_path = tmp / "composition.json"
            comp_path.write_text(json.dumps({"duration_s": b["duration"], "title": b["title"]}))

            # Create fake composition.zip
            zip_path = tmp / "composition.zip"
            zip_path.write_bytes(b"fake zip content")

            # Create tarball
            with tarfile.open(tar_path, "w:gz") as tar:
                tar.add(mp4_path, arcname="out.mp4")
                tar.add(comp_path, arcname="composition.json")
                tar.add(zip_path, arcname="composition.zip")

            # compute hashes while files still exist
            h_md5 = hashlib.md5(mp4_path.read_bytes()).hexdigest()

        # compute hash for the tarball itself
        h_sha256 = hashlib.sha256(tar_path.read_bytes()).hexdigest()

        readme_path.write_text(
            f"Bundle: {bundle_name}\n"
            f"MP4 MD5: {h_md5}\n"
            f"Bundle SHA256: {h_sha256}\n"
        )
        print(f"Created {tar_path} (MD5: {h_md5}, SHA256: {h_sha256})")

if __name__ == "__main__":
    main()
