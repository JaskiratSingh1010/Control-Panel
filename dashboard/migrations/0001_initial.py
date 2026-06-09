from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name='ExpenseBudget',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('budget_head', models.CharField(max_length=100)),
                ('month', models.IntegerField()),
                ('year', models.IntegerField()),
                ('budget_amount', models.FloatField(default=0)),
            ],
            options={
                'unique_together': {('budget_head', 'month', 'year')},
            },
        ),
    ]
