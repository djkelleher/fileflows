import fnmatch
import gzip
import re
from functools import cached_property
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Sequence, Tuple, Union

import boto3
import polars as pl
from boto3.session import Config
from botocore.exceptions import ClientError
from pyarrow.fs import S3FileSystem
from pydantic import AnyHttpUrl, SecretStr
from pydantic_settings import BaseSettings
from tqdm import tqdm

from .extensions import file_extensions_re

PathT = Union[str, Path]


class S3Cfg(BaseSettings):
    """S3 configuration. Variables will be loaded from environment variables if set."""

    s3_endpoint_url: AnyHttpUrl = "http://localhost:9000"
    aws_access_key_id: str
    aws_secret_access_key: SecretStr


class S3Ops:
    """File operations for s3 protocol object stores."""

    _bucket_and_partition_re = re.compile(r"s3:\/\/([-\w]+)(?:\/(.+))?")

    def __init__(self, s3_cfg: Optional[S3Cfg] = None) -> None:
        self.s3_params = s3_cfg or S3Cfg()

    def is_s3_path(self, path: PathT) -> bool:
        """Returns True if `path` is an s3 path."""
        return str(path).startswith("s3://")

    def upload(
        self,
        files: Union[PathT, Sequence[PathT]],
        bucket_name: str,
        partition_relative_to: Optional[str] = None,
    ):
        """Upload a local file or files to a bucket.

        Args:
            files (Union[PathT, Sequence[PathT]]): Local file or files to upload. (Note: easily get list of local files via `Path.glob`, `Path.rglob`, or `Path.iterdir`)
            bucket_name (str): Bucket to upload to.
            partition_relative_to (Optional[str], optional): Use part of `file` path relative to `partition_relative_to` as s3 partition. If literal "bucket_name", path relative to `bucket_name` arg will be used. Defaults to None.
        """
        files = [files] if isinstance(files, str) else files
        for file in files:
            partition = (
                str(file).split(partition_relative_to)[-1].lstrip("/")
                if partition_relative_to
                else file
            )
            self.client.upload_file(str(file), bucket_name, partition)

    def read_file(self, path: str) -> BytesIO:
        """Read a file from s3.

        Args:
            path (str): Path in S3.

        Returns:
            BytesIO: The downloaded file contents.
        """
        bucket_name, partition = self.bucket_and_partition(path)
        buffer = BytesIO()
        self.client.download_fileobj(bucket_name, partition, buffer)
        buffer.seek(0)
        return buffer

    def download_file(
        self, s3_path: str, local_path: PathT, overwrite: bool = True
    ) -> bool:
        """Download a file from s3.

        Args:
            s3_path (str): The file to download.
            local_path (PathT): A local file path or directory to save the file to. If a directory is provided, any subdirectories in `s3_path` partition will be created with the `local_path` as the root.
            overwrite (bool, optional): Overwrite file if it already exists. Defaults to True.

        Returns:
            bool: True if file was downloaded, False if file already exists and `overwrite` is False.
        """

        local_path = Path(local_path)
        if not local_path.suffix:
            # this is a directory.
            local_path = local_path.joinpath(s3_path.replace("s3://", ""))
        local_path.parent.mkdir(exist_ok=True, parents=True)

        if overwrite or not local_path.exists():
            bucket, partition = self.bucket_and_partition(s3_path)
            with local_path.open(mode="wb+") as f:
                self.client.download_fileobj(bucket, partition, f)
            return True
        return False

    def download_files(
        self,
        bucket_name: str,
        save_dir: Path,
        partition: Optional[str] = None,
        overwrite: bool = True,
    ):
        """Download files from s3.

        Args:
            bucket_name (str): Name of bucket where files are located.
            save_dir (Path): Local directory where files should be downloaded to.
            partition (Optional[str], optional): Only download files in this partition. Defaults to None.
            overwrite (bool, optional): Overwrite existing local files. Defaults to True.
        """
        for file in tqdm(self.list_files(bucket_name, partition, return_as="urls")):
            self.download_file(file, save_dir, overwrite)

    def delete_file(self, file: str, if_exists: bool = False):
        """Delete a file from s3.

        Args:
            file (str): URL of file in s3.
            if_exists (bool, optional): Do not raise exception if file does not exist. Defaults to False.
        """
        bucket_name, partition = self.bucket_and_partition(file)
        if not partition:
            if if_exists:
                return
            raise FileNotFoundError(f"File does not exist: {file}")
        try:
            self.client.delete_object(
                Bucket=bucket_name,
                Key=partition,
            )
        except ClientError as err:
            if err.response["Error"]["Code"] == "404":
                if not if_exists:
                    raise err
            else:
                raise err

    def delete_files(
        self, bucket_name: str, partition: Optional[str] = None, if_exists: bool = False
    ):
        """Delete files from a bucket or bucket partition.

        Args:
            bucket_name (str): Bucket who's files should be deleted.
            partition (Optional[str], optional): Only delete this partition withing the bucket. Defaults to None.
            if_exists (bool, optional): Do not raise exception if file does not exist. Defaults to False.
        """
        for file in tqdm(self.list_files(bucket_name, partition, return_as="paths")):
            try:
                self.client.delete_object(
                    Bucket=bucket_name,
                    Key=file,
                )
            except ClientError as err:
                if err.response["Error"]["Code"] == "404":
                    if not if_exists:
                        raise err
                else:
                    raise err

    def transfer_s3_location(self, src_path: str, dst_path: str, delete_src: bool):
        """Move files in s3 to another location in s3.
            - move a file to new partition,
            - move a file to a new file,
            - move a partition to a new partition,

        Args:
            src_path (str): Source file or partition.
            dst_path (str): Destination file or partition.
            delete_src (bool): Remove the content at src_path after transferring.
        """
        src_bucket_name, src_partition = self.bucket_and_partition(
            src_path, require_partition=False
        )
        dst_bucket_name, dst_partition = self.bucket_and_partition(
            dst_path, require_partition=False
        )
        src_files = self.list_files(src_bucket_name, src_partition, return_as="paths")
        src_partition_is_file = file_extensions_re.search(src_partition)
        dst_partition_is_file = file_extensions_re.search(dst_partition)

        dst_bucket = self.resource.Bucket(dst_bucket_name)

        if src_partition_is_file:
            assert len(src_files) == 1
            src_file = src_files[0]
            if not dst_partition_is_file:
                # create file path in partition.
                dst_path = f"{dst_partition}/{src_file.split('/')[-1]}"
            else:
                dst_path = dst_path.split(f"{dst_bucket_name}/")[-1]
            # copy src file to dst file.
            dst_bucket.Object(dst_path).copy(
                {"Bucket": src_bucket_name, "Key": src_file}
            )
        else:
            if dst_partition_is_file:
                raise ValueError(
                    f"Cannot move a partition to a file. Partition: {src_path}, File: {dst_path}"
                )
            # loop over all files in source partition.
            for src_file in tqdm(src_files):
                dst_path = f"{dst_partition}/{src_file.split('/')[-1]}"
                dst_bucket.Object(dst_path).copy(
                    {"Bucket": src_bucket_name, "Key": src_file}
                )
        if delete_src:
            self.client.delete_object(
                Bucket=src_bucket_name,
                Key=src_partition,
            )

    def exists(self, file: str) -> bool:
        """Check if a file exists in s3."""
        bucket, partition = self.bucket_and_partition(file, require_partition=False)
        if not partition:
            # check if bucket exists.
            try:
                self.client.head_bucket(Bucket=bucket)
                return True
            except ClientError as e:
                if e.response["Error"]["Code"] == "404":
                    # The bucket does not exist.
                    return False
                raise
        try:
            self.client.head_object(Bucket=bucket, Key=partition)
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "404":
                # The object does not exist.
                return False
            raise

    def file_size(self, file: str) -> int:
        """Get the size of a file in bytes."""
        bucket, partition = self.bucket_and_partition(file)
        return self.resource.Object(bucket, partition).content_length

    def get_bucket(self, bucket_name: str):
        """Get a bucket object for `bucket_name`. If bucket does not exist, create it."""
        bucket_name = re.sub(r"^s3:\/\/", "", bucket_name)
        bucket = self.resource.Bucket(bucket_name)
        if not bucket.creation_date:
            try:
                bucket.create()
            except ClientError as err:
                if err.response["Error"]["Code"] != "BucketAlreadyOwnedByYou":
                    raise err
        return bucket

    def list_buckets(self, pattern: Optional[str] = None) -> List[str]:
        """Names of all buckets on server. Optionally keep only buckets matching `pattern`."""
        buckets = [b["Name"] for b in self.client.list_buckets()["Buckets"]]
        if pattern:
            return fnmatch.filter(pattern, buckets)
        return buckets

    def list_files(
        self,
        bucket_name: str,
        partition: Optional[str] = None,
        return_as: Literal["names", "paths", "urls", "obj"] = "urls",
        pattern: Optional[str] = None,
    ) -> List[str]:
        """List all files in a bucket.

        Args:
            bucket_name (str): Bucket to search in.
            partition (Optional[str], optional): Only return files in this partition. Defaults to None.
            return_as (Literal["names", "paths", "urls", "obj"], optional): How files should be returned. Defaults to "urls".
        """
        bucket = self.get_bucket(bucket_name)
        files = list(bucket.objects.all())
        if pattern:
            files = [f for f in files if fnmatch.fnmatch(f.key, pattern)]
        if partition:
            # filter out files that are not in this partition.
            partition_re = re.compile(re.escape(partition) + r"(\/|$)")
            files = [f for f in files if partition_re.search(f.key)]
        if return_as == "obj":
            return files
        files = [f.key for f in files]
        if return_as == "paths":
            return files
        if return_as == "urls":
            return [f"s3://{bucket.name}/{f}" for f in files]
        if return_as == "names":
            return [f.split("/")[-1] for f in files]
        raise ValueError(f"Invalid return format: {return_as}")

    def df_from_csv(
        self,
        path: str,
        header: Union[bool, Sequence[str]],
        dtypes: Dict[str, Any] = None,
        return_as: Literal["pandas", "polars"] = "pandas",
    ):
        """Create DataFrame from CSV file in S3."""
        has_header = header is True
        if path.endswith(".gz"):
            df = pl.read_csv(
                gzip.decompress(self.read_file(path).read()),
                has_header=has_header,
                dtypes=dtypes,
            )
        else:
            df = pl.read_csv(
                path,
                storage_options=self.storage_options,
                has_header=has_header,
                dtypes=dtypes,
            )
        if isinstance(header, (list, tuple)):
            assert len(header) == len(df.columns)
            df = df.rename(dict(zip(df.columns, header)))
        if return_as == "pandas":
            return df.to_pandas()
        return df

    def df_from_parquet(
        self,
        path: str,
        return_as: Literal["pandas", "polars"] = "pandas",
    ):
        """Create DataFrame from parquet file in s3."""
        df = pl.read_parquet(path, storage_options=self.storage_options)
        if return_as == "pandas":
            return df.to_pandas()
        return df

    def bucket_and_partition(
        self, path: str, require_partition: bool = True
    ) -> Union[None, Tuple[str, str]]:
        """Split a s3 path into bucket and partition."""
        if match := self._bucket_and_partition_re.search(path):
            # bucket name, partition
            bucket = match.group(1)
            partition = match.group(2)
            if require_partition and not partition:
                raise ValueError(f"Path {path} does not contain a partition: {path}")
            return bucket, partition

    @cached_property
    def arrow_fs(self) -> S3FileSystem:
        return S3FileSystem(
            access_key=self.s3_params.aws_access_key_id,
            secret_key=self.s3_params.aws_secret_access_key.get_secret_value(),
            endpoint_override=self.s3_params.s3_endpoint_url,
        )

    @cached_property
    def storage_options(self):
        return {
            "key": self.s3_params.aws_access_key_id,
            "secret": self.s3_params.aws_secret_access_key.get_secret_value(),
            "client_kwargs": {"endpoint_url": self.s3_params.s3_endpoint_url},
        }

    @cached_property
    def resource(self):
        return self._boto3_obj("resource")

    @cached_property
    def client(self):
        return self._boto3_obj("client")

    def _boto3_obj(self, obj_type: Literal["resource", "client"]):
        return getattr(boto3, obj_type)(
            "s3",
            endpoint_url=self.s3_params.s3_endpoint_url,
            aws_access_key_id=self.s3_params.aws_access_key_id,
            aws_secret_access_key=self.s3_params.aws_secret_access_key.get_secret_value(),
            config=Config(signature_version="s3v4"),
        )
