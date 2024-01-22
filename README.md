Core functions are accessed through the `Files` class. This is a high-level interface for file operations on your local file system and/or s3 protocol object stores. Internally, appropriate functions will by called based on the type of arguments passed. i.e. `s3://` path or `/local/path`.

```py
from fileflows import Files
```
Core functions include `create, transfer, copy, move, delete, exists, file_size, list_files, parquet_column_names, df_from_csv, df_from_parquet`.
See [core](/fileflows/core.py) for more details.   

Additional functionality specific to s3 protocol object stores can be accessed through the `S3` class. See [s3](/fileflows/s3.py) for more details.
```py
from fileflows import S3
```

Both `Files` and `S3` take an optional `S3Cfg` object as argument (`from fileflows import S3Cfg`). `S3Cfg` contains parameters for the s3 protocol object store you are working with. If no argument is provided, `S3Cfg` will default to environment variables: `s3_endpoint_url` (http://localhost:9000 by default), `aws_access_key_id`, `aws_secret_access_key`.