# -*- coding: utf-8 -*-
# Generated by Django 1.10.8 on 2018-05-25 17:01
from __future__ import unicode_literals

import django.db.models.deletion
import django.utils.timezone
import model_utils.fields
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('locations', '0003_make_not_nullable'),
        ('contenttypes', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='cartodbtable',
            name='remap_table_name',
            field=models.CharField(blank=True, max_length=254, null=True, verbose_name='Remap Table Name'),
        ),
        migrations.AddField(
            model_name='location',
            name='is_active',
            field=models.BooleanField(default=True, verbose_name='Active'),
        ),
        migrations.CreateModel(
            name='LocationRemapHistory',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('object_id', models.IntegerField(verbose_name='Object ID')),
                ('comments', models.TextField(blank=True, null=True, verbose_name='Comments')),
                ('created', model_utils.fields.AutoCreatedField(default=django.utils.timezone.now, editable=False,
                                                                verbose_name='created')),
                ('content_type',
                 models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='contenttypes.ContentType',
                                   verbose_name='Content Type')),
                ('new_location', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='+',
                                                   to='locations.Location', verbose_name='New Location')),
                ('old_location', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='+',
                                                   to='locations.Location', verbose_name='Old Location')),
            ],
        ),
    ]
