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
