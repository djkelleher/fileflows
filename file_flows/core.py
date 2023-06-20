import os
import shutil
from functools import cached_property
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Sequence, Union

import polars as pl
from pyarrow import parquet

from .s3 import S3Cfg, S3Ops


class FileOps:
    """File operations for local file system and/or s3 protocol object stores."""

    def __init__(self, s3_cfg: Optional[S3Cfg] = None) -> None:
        self._s3_cfg = s3_cfg

    def create(self, location: Union[str, Path]):
        """Create a directory or bucket if it doesn't already exist."""
        # make sure primary save location exists.
        if self.s3.is_s3_path(location):
            # make sure bucket exists.
            bucket_name, _ = self.s3.bucket_and_partition(
                location, require_partition=False
            )
            self.s3.get_bucket(bucket_name)
        else:
            # make sure directory exists.
            Path(location).mkdir(exist_ok=True, parents=True)

    def transfer(
        self,
        src_path: Union[str, Path],
        dst_path: Union[str, Path],
        delete_src: bool = False,
    ):
        """Move or copy file to a new location."""

        if self.s3.is_s3_path(src_path):
            if self.s3.is_s3_path(dst_path):
                self.s3.transfer_s3_location(
                    src_path=src_path, dst_path=dst_path, delete_src=delete_src
                )
            else:
                if os.path.isdir(dst_path):
                    dst_path = f"{dst_path}/{Path(src_path).name}"
                self.s3.download_file(
                    s3_path=src_path,
                    local_path=dst_path,
                    overwrite=True,
                )

        elif self.s3.is_s3_path(dst_path):
            # upload local file to s3.
            bucket_name, partition = self.s3.bucket_and_partition(
                dst_path, require_partition=False
            )
            if not partition:
                partition = str(src_path).split(f"{bucket_name}/")[-1].lstrip("/")
            self.s3.client.upload_file(str(src_path), bucket_name, partition)
        else:
            shutil.copy(src_path, dst_path)
        if delete_src:
            self.delete(src_path)

    def copy(self, src_path: Union[str, Path], dst_path: Union[str, Path]):
        """Copy file to a new location."""
        return self.transfer(src_path, dst_path, delete_src=False)

    def move(self, src_path: Union[str, Path], dst_path: Union[str, Path]):
        """Move file to a new location."""
        return self.transfer(src_path, dst_path, delete_src=True)

    def delete(self, file: Union[str, Path], if_exists: bool = False):
        """Delete file."""
        if self.s3.is_s3_path(file):
            return self.s3.delete_file(file, if_exists=if_exists)
        try:
            Path(file).unlink()
        except FileNotFoundError:
            if not if_exists:
                raise

    def exists(self, file: Union[str, Path]) -> bool:
        """Returns True if file exists."""
        if self.s3.is_s3_path(file):
            return self.s3.exists(file)
        return os.path.exists(file)

    def file_size(self, file: Union[str, Path]) -> int:
        """Returns file size in bytes."""
        if self.s3.is_s3_path(file):
            return self.s3.file_size(file)
        return os.path.getsize(file)

    def list_files(
        self, directory: Union[str, Path], pattern: Optional[str] = None
    ) -> Union[List[Path], List[str]]:
        """Returns list of files in directory."""
        if self.s3.is_s3_path(directory):
            return self.s3.list_files(directory, pattern=pattern)
        if pattern:
            return list(Path(directory).glob(pattern))
        return list(Path(directory).iterdir())

    def parquet_column_names(self, file: Union[str, Path]) -> List[str]:
        """Returns list of column names in parquet file."""
        return list(
            parquet.read_schema(
                file,
                filesystem=self.s3.arrow_fs() if self.s3.is_s3_path(file) else None,
            ).names
        )

    def df_from_csv(
        self,
        path: str,
        header: Union[bool, Sequence[str]],
        dtypes: Dict[str, Any] = None,
        return_as: Literal["pandas", "polars"] = "pandas",
    ):
        """Create DataFrame from CSV file in S3."""
        if self.s3.is_s3_path(path):
            return self.s3.df_from_csv(path, header, dtypes, return_as)
        df = pl.read_csv(
            path,
            dtypes=dtypes,
            has_header=header is True,
            columns=header if isinstance(header, (list, tuple)) else None,
        )
        if return_as == "pandas":
            return df.to_pandas()
        return df

    def df_from_parquet(
        self,
        path: str,
        return_as: Literal["pandas", "polars"] = "pandas",
    ):
        """Create DataFrame from parquet file in s3."""
        if self.s3.is_s3_path(path):
            return self.s3.df_from_parquet(path, return_as)
        df = pl.read_parquet(path)
        if return_as == "pandas":
            return df.to_pandas()
        return df

    @cached_property
    def s3(self) -> S3Ops:
        return S3Ops(self._s3_cfg)
