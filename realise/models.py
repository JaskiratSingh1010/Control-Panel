from django.conf import settings
from django.db import models


class MonthlyTarget(models.Model):
    PRODUCT_TYPES = [('PREMIUM', 'Premium'), ('COMMODITY', 'Commodity')]

    product_type = models.CharField(max_length=20, choices=PRODUCT_TYPES)
    sub_group    = models.CharField(max_length=100)
    month        = models.IntegerField()
    year         = models.IntegerField()
    tgt_ltrs     = models.FloatField(default=0)
    tgt_rate     = models.FloatField(default=0)
    updated_at   = models.DateTimeField(auto_now=True)
    updated_by   = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='realise_targets',
    )

    class Meta:
        unique_together = ('product_type', 'sub_group', 'month', 'year')
        ordering = ['-year', '-month', 'product_type', 'sub_group']
        indexes = [
            models.Index(fields=['year', 'month']),
            models.Index(fields=['product_type', 'sub_group']),
        ]

    @property
    def key(self):
        return f"{self.product_type}|{self.sub_group}"

    def __str__(self):
        return f"{self.key} {self.month}/{self.year}"


class MainGroupMaster(models.Model):
    name = models.CharField(max_length=50, unique=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class StateMaster(models.Model):
    name = models.CharField(max_length=100, unique=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class TargetMaster(models.Model):
    main_group = models.ForeignKey(MainGroupMaster, on_delete=models.CASCADE)
    state = models.ForeignKey(StateMaster, null=True, blank=True, on_delete=models.SET_NULL)
    sales_person = models.CharField(max_length=100, null=True, blank=True)
    target_ltrs = models.DecimalField(max_digits=12, decimal_places=2)
    month = models.IntegerField()
    year = models.IntegerField()

    class Meta:
        unique_together = ('main_group', 'state', 'sales_person', 'month', 'year')
        ordering = ['-year', '-month', 'main_group__name', 'state__name', 'sales_person']
        indexes = [
            models.Index(fields=['year', 'month']),
            models.Index(fields=['main_group']),
        ]

    def __str__(self):
        state = self.state.name if self.state_id else 'ALL'
        sales_person = self.sales_person or 'ALL'
        return f"{self.main_group.name} {state} {sales_person} {self.month}/{self.year}"


class SegmentTarget(models.Model):
    """Flat per-value target for a single dimension (main group, state or person)."""

    SEGMENT_TYPES = [
        ('main_group', 'Main Group'),
        ('state', 'State'),
        ('person', 'Person'),
        ('premium_item', 'Premium Items'),
        ('commodity_item', 'Commodity Items'),
    ]

    segment_type = models.CharField(max_length=20, choices=SEGMENT_TYPES)
    segment_value = models.CharField(max_length=100)
    month = models.IntegerField()
    year = models.IntegerField()
    target_ltrs = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    target_realise_value = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('segment_type', 'segment_value', 'month', 'year')
        ordering = ['segment_type', 'segment_value']
        indexes = [
            models.Index(fields=['segment_type', 'year', 'month']),
        ]

    def __str__(self):
        return f"{self.segment_type}:{self.segment_value} {self.month}/{self.year}"


class TargetNode(models.Model):
    """Free-form hierarchical target. Any of the three dimensions may be blank,
    so a target can be held at any level (e.g. GT only, or GT+Punjab, or GT+Punjab+Prince).
    Blank ('') = that dimension is not part of this node. No auto-splitting."""

    main_group = models.CharField(max_length=50, blank=True, default='')
    state = models.CharField(max_length=100, blank=True, default='')
    sales_person = models.CharField(max_length=100, blank=True, default='')
    segment = models.CharField(max_length=20, blank=True, default='')  # '' = all, PREMIUM, COMMODITY
    month = models.IntegerField()
    year = models.IntegerField()
    target_ltrs = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    target_realise = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('main_group', 'state', 'sales_person', 'segment', 'month', 'year')
        ordering = ['main_group', 'state', 'sales_person']
        indexes = [
            models.Index(fields=['year', 'month']),
        ]

    def __str__(self):
        combo = '+'.join([p for p in (self.main_group, self.state, self.sales_person) if p]) or 'ALL'
        return f"{combo} {self.month}/{self.year}"
