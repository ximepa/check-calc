"""Receipts can now be PDFs as well as photos, so the field is a FileField."""

import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("checks", "0002_receiptupload"),
    ]

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
                help_text="A photo of the receipt, or a PDF scan (all pages of one receipt).",
                upload_to="receipts/%Y/%m/",
                validators=[
                    django.core.validators.FileExtensionValidator(
                        ["jpg", "jpeg", "png", "webp", "gif", "bmp", "tif", "tiff", "pdf"]
                    )
                ],
            ),
        ),
    ]
