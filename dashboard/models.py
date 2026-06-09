from django.db import models


class ExpenseBudget(models.Model):
    budget_head = models.CharField(max_length=100)
    month = models.IntegerField()
    year = models.IntegerField()
    budget_amount = models.FloatField(default=0)

    class Meta:
        unique_together = ('budget_head', 'month', 'year')
