"""Accept PDFs as well as photos, and record which backend read them."""

import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("checks", "0002_receiptupload")]

    operations = [
        migrations.RenameField(
            model_name="receiptupload",
            old_name="image",
            new_name="document",
        ),
        migrations.AlterField(
            model_name="receiptupload",
            name="document",
            field=models.FileField(
                help_text="Photo, scan or PDF of the receipt.",
                upload_to="receipts/%Y/%m/",
                validators=[
                    django.core.validators.FileExtensionValidator(
                        ["jpg", "jpeg", "png", "webp", "gif", "bmp", "tif", "tiff", "pdf"]
                    )
                ],
            ),
        ),
        migrations.AddField(
            model_name="receiptupload",
            name="backend",
            field=models.CharField(blank=True, editable=False, max_length=32),
        ),
    ]
