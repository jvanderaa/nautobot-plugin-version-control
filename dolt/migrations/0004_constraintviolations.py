# Generated by Django 3.1.13 on 2021-07-30 15:58

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dolt", "0003_conflicts"),
    ]

    operations = [
        migrations.CreateModel(
            name="ConstraintViolations",
            fields=[
                ("table", models.TextField(primary_key=True, serialize=False)),
                ("num_violations", models.IntegerField()),
            ],
            options={
                "verbose_name_plural": "constraint violations",
                "db_table": "dolt_constraint_violations",
                "managed": False,
            },
        ),
    ]
