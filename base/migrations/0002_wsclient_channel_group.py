# Generated by Django 4.2.2 on 2023-06-29 21:38

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("base", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="wsclient",
            name="channel_group",
            field=models.CharField(default="symbols_html", max_length=256),
            preserve_default=False,
        ),
    ]
