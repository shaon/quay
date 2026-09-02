from datetime import datetime, timedelta

from data import model
from data.database import BlobUpload as BlobUploadTable
from workers.blobuploadcleanupworker.models_interface import (
    BlobUpload,
    BlobUploadCleanupWorkerDataInterface,
)


class PreOCIModel(BlobUploadCleanupWorkerDataInterface):
    def get_blob_upload_max_id(self):
        return model.blob.get_blob_upload_max_id()

    def get_stale_blob_upload(self, stale_before, after_upload_id, max_upload_id):
        blob_upload = model.blob.get_stale_blob_upload(stale_before, after_upload_id, max_upload_id)
        if blob_upload is None:
            return None

        return BlobUpload(
            blob_upload.id,
            blob_upload.uuid,
            blob_upload.storage_metadata,
            blob_upload.location.name,
            blob_upload.created,
        )

    def delete_blob_upload(self, blob_upload):
        BlobUploadTable.delete().where(
            BlobUploadTable.id == blob_upload.id,
            BlobUploadTable.uuid == blob_upload.uuid,
            BlobUploadTable.created == blob_upload.created,
        ).execute()

    def create_stale_upload_for_testing(self):
        blob_upload = model.blob.initiate_upload("devtable", "simple", "foobarbaz", "local_us", {})
        blob_upload.created = datetime.now() - timedelta(days=60)
        blob_upload.save()
        return BlobUpload(
            blob_upload.id,
            blob_upload.uuid,
            blob_upload.storage_metadata,
            blob_upload.location.name,
            blob_upload.created,
        )

    def blob_upload_exists(self, upload_uuid):
        blob_upload = model.blob.get_blob_upload_by_uuid(upload_uuid)
        return blob_upload is not None


pre_oci_model = PreOCIModel()
