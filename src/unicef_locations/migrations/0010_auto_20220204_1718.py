# Generated by Django 4.0 on 2022-02-04 17:18

from django.db import migrations


def data_migration(apps, schema_editor):
    Location = apps.get_model("locations", "Location")
    CartoDBTable = apps.get_model("locations", "CartoDBTable")

    for loc in Location.objects.all():
        loc.admin_level_name = loc.gateway.name
        loc.admin_level = loc.gateway.admin_level
        loc.save()

    for table in CartoDBTable.objects.all():
        table.admin_level_name = table.admin_level_name.name
        table.admin_level = table.location_type.admin_level
        table.save()


class Migration(migrations.Migration):

    dependencies = [
        ('locations', '0009_cartodbtable_admin_level_and_more'),
    ]

    operations = [
        migrations.RunPython(data_migration, migrations.RunPython.noop)
    ]