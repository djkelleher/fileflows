import ast
import logging
import multiprocessing
import os
import pathlib
import re
import sys
import time
from typing import List, Union

import googleapiclient.errors
import redis
from pydrive.auth import GoogleAuth
from pydrive.drive import GoogleDrive
from pydrive.files import GoogleDriveFileList

logging.basicConfig(format='%(asctime)s %(message)s',level=logging.INFO)
logger = logging.getLogger("GDriveClient")

# Connect to Redis server.
redis_client = redis.Redis(host='localhost', port=6379, db=0, charset="utf-8", decode_responses=True)

# Authenticate with google drive.
gauth = GoogleAuth()
# Create local webserver and auto handles authentication.
gauth.LocalWebserverAuth()
drive_client = GoogleDrive(gauth)


def set_queue(src_dirs: List[pathlib.Path], redis_set_key: str="gdrive", glob_search: str=None, re_search: str=None):   
    if not isinstance(src_dirs, list): src_dirs = [src_dirs]
    # If not both glob and re constaints are not given, take all the files.
    if glob_search is None and re_search is None:
        for ff in src_dirs:
            for f in ff.iterdir(): redis_client.sadd(redis_set_key,str(f))
        logger.info(f"Added {redis_client.scard(redis_set_key)} files to queue from src dir iteration.")
    # find files based on glob match.
    if glob_search is not None:
        for ff in src_dirs:
            for f in ff.glob(glob_search): redis_client.sadd(redis_set_key,str(f))
        logger.info(f"Added {redis_client.scard(redis_set_key)} files were added to queue from glob search {glob_search}.")
    # find files based on regular expression match.
    if re_search is not None:
        start_size = redis_client.scard(redis_set_key)
        for ff in src_dirs:
            matches = [f for f in ff.iterdir() if re.search(re_search,f) is not None]
            for f in matches: redis_client.sadd(redis_set_key,str(f))
        if redis_client.scard(redis_set_key) == start_size:
            logger.warning(f"No files were added to queue from re search {re_search}.")
    logger.info(f"Total set size for key {redis_set_key}: {redis_client.scard(redis_set_key)}")

def create_gdrive_folder(dirve_client, folder_name, parent_folder_id):
    folder_obj = drive_client.CreateFile(
        {
            'title': folder_name,
            # Define the file type as folder
            'mimeType': 'application/vnd.google-apps.folder',
            # ID of the parent folder
            'parents': [{"kind": "drive#fileLink", "id": parent_folder_id}]
        }
    )
    folder_obj.Upload()
    logger.info(f"Created new folder ({folder_name}) with id {folder_obj['id']}, parent id {parent_folder_id}")
    return folder_obj['id']

# Check if destination folder exists and return it's ID.
def get_folder_id(drive_client, folder_path: List[str]):
    dst_folder_name = folder_path[-1]
    logger.info(f"Searching for folder id for folder: {dst_folder_name}")
    def search_file_tree(parent_folder_id):
        if len(folder_path) > 0:
            folder_name = folder_path.pop(0)
            logger.info(f"Searching for folder: {folder_name}")
            try:
                file_list = drive_client.ListFile(
                    {'q': f"'{parent_folder_id}' in parents and trashed=false"}
                ).GetList()
            except googleapiclient.errors.HttpError as e:
                message = ast.literal_eval(e.content)['error']['message']
                if "file not found" in message.lower():
                    logger.error(f"Folder {folder_name} was not found on Google Drive. Error: {message}")
                    sys.exit(1)
                else:
                    logger.exception(message)
            # Find the the destination folder in the parent folder's files.
            for file in file_list:
                if file['title'] == folder_name:
                    if folder_name == dst_folder_name: return file['id']
                    search_file_tree(file['id']) # recurse to next level if the folder was found.
            # create the folder if it is not found.
            folder_id = create_gdrive_folder(drive_client, folder_name, parent_folder_id)
            if folder_name == dst_folder_name: return folder_id
            search_file_tree(folder_id)
    return search_file_tree('root')

def upload(dst_folder: List[str], overwrite_existing=False, num_proc=4, redis_set_key: str="gdrive"):
    dst_folder_id = get_folder_id(drive_client, dst_folder)
    if overwrite_existing == False:
        set_card_before_rem = redis_client.scard(redis_set_key)
        existing_files = drive_client.ListFile({'q': f"'{dst_folder_id}' in parents and trashed=false"}).GetList()
        logger.info(f"Found {len(existing_files)} existing files in destination folder {dst_folder}.")
        for file in existing_files:
            redis_client.srem(redis_set_key,file['title'])
        num_files_removed = redis_client.scard(redis_set_key)-set_card_before_rem
        logger.info(f"{num_files_removed} files in {redis_set_key} are already in destination folder {dst_folder}. Upload will be skipped.")
    def upload_file():
        while True:
            file = pathlib.Path(redis_client.spop(redis_set_key))
            print(f'process {os.getpid()} uploading file {file}')
            if file is None:
                logger.info(f"None returned from spop on key: {redis_set_key}. Break.")
                break
            f=drive_client.CreateFile(
                {'title': file.name, "parents": [{"kind": "drive#fileLink", "id": dst_folder_id}]}
            )
            f.SetContentFile(str(file))
            f.Upload()
    for _ in range(num_proc):
        p = multiprocessing.Process(target=upload_file)
        p.start()
        #p.join()
        print("PROC CREATED")

def delete(drive_client, redis_client, dst_folder: List[str], num_proc=4, redis_set_key: str="gdrive"):
    dst_folder_id = get_folder_id(drive_client, dst_folder)
    dst_files = set(drive_client.ListFile({'q': f"'{dst_folder_id}' in parents and trashed=false"}).GetList())
    def delete_file():
        while True:
            file = redis_client.spop(redis_set_key)
            if file is None:
                logger.info(f"None returned from spop on key: {redis_set_key}. Break.")
                break
            dst_file = dst_files.pop(file)
            if dst_file is not None:
                dst_file.Trash()
    for _ in range(num_proc):
        p = multiprocessing.Process(target=delete_file)
        p.start()
        p.join()

def rename_files(drive_client, folder, rename_func):
    folder_id = get_folder_id(drive_client, folder)
    print(f"Found folder id for folder {folder}: {folder_id}")
    folder_files = drive_client.ListFile({'q': f"'{folder_id}' in parents and trashed=false"}).GetList()
    print(f"Found {len(folder_files)} total files in folder {folder}")
    folder_files = [f for f in folder_files if "/" in f['title']]
    print(f"Found {len(folder_files)} files with absolute file path names.")
    for ff in folder_files:
        old_title = ff['title']
        f = drive_client.CreateFile({'id': ff['id']})
        f['title'] = old_title.split("/")[-1]
        print(f"Updating file title: {f['title']}")
        f.Upload()



def drive_upload(http):
    drive_service = discovery.build("drive", "v3", http=http)
    file_metadata = {
        "name": "AcademiaDasApostas",
        "mimeType": "application/vnd.google-apps.spreadsheet",
    }

    media = MediaFileUpload("bets.txt", mimetype="text/csv", resumable=True)
    file = (
        drive_service.files()
        .update(fileId=file_id, body=file_metadata, media_body=media, fields="id")
        .execute()
    )
    print(file)
