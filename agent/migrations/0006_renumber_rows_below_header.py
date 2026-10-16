from django.db import migrations
from django.db.models import F


def shift_down(apps, schema_editor):
    """Rows were numbered as Excel rows (the first claim was 2, because row 1
    is the header). They are now counted from the first row under the
    header, so every existing batch row moves down by one."""
    CallLog = apps.get_model("agent", "CallLog")
    CallLog.objects.filter(row_number__isnull=False).update(row_number=F("row_number") - 1)


def shift_up(apps, schema_editor):
    CallLog = apps.get_model("agent", "CallLog")
    CallLog.objects.filter(row_number__isnull=False).update(row_number=F("row_number") + 1)


class Migration(migrations.Migration):

    dependencies = [
        ("agent", "0005_verified_status_and_batch_number"),
    ]

    operations = [
        migrations.RunPython(shift_down, shift_up),
    ]
