import re

file_extensions = "|".join(
    [
        "csv",
        "gz",
        "parquet",
        "zip",
        "json",
        "txt",
        "tsv",
        "xls",
        "xlsx",
        "xml",
        "html",
        "htm",
        "yml",
        "yaml",
        "hdf",
        "hdf5",
        "h5",
        "feather",
        "pkl",
        "pickle",
    ]
)

file_extensions_re = re.compile(r"\.(" + file_extensions + r")$")
