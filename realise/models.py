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


class TerritoryMapping(models.Model):
    """Editable person-ownership of each (channel, state) territory cell.

    The grid identity — channel, state_code, state_name — is FIXED (seeded from
    live SAP data; channels are the 7 dashboard channels GT/MT/ROI/ECOM/HORECA/
    CSD/REST). The ONLY user-editable field is ``sales_person``. This table is the
    DB-backed successor to ``services.TERRITORY_SHEET``: it drives the dashboard's
    person attribution, Order-in-Hand-by-person roll-up, GT/MT state whitelist and
    the Update Targets editor rows. Premium/Commodity targets live elsewhere
    (TargetNode) — they are not edited here.

    A blank ``state_name`` row is a channel-level (national) owner, e.g. CSD or
    E-Commerce that resolve to one person regardless of state.
    """

    channel      = models.CharField(max_length=20)               # GT, MT, ROI, ECOM, HORECA, CSD, REST
    state_code   = models.CharField(max_length=10, blank=True, default='')   # DL, PB… ('' = national)
    state_name   = models.CharField(max_length=100, blank=True, default='')  # DELHI, PUNJAB… ('' = national)
    sales_person = models.CharField(max_length=100, blank=True, default='')  # the only editable field
    updated_at   = models.DateTimeField(auto_now=True)
    updated_by   = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='territory_mappings',
    )

    class Meta:
        unique_together = ('channel', 'state_name')
        ordering = ['channel', 'state_name']
        indexes = [
            models.Index(fields=['channel']),
        ]

    def __str__(self):
        cell = f'{self.channel} {self.state_name or "(national)"}'
        return f'{cell} → {self.sales_person or "—"}'


class CityOwner(models.Model):
    """City/district-level ASM override inside a (channel, state) territory. Lets a
    territory be split among MULTIPLE ASMs: a sale whose ship-to city matches a CityOwner
    is attributed to that ASM; cities with no CityOwner fall back to the territory's
    default owner in TerritoryMapping. (channel, state_name, city) is unique."""

    channel      = models.CharField(max_length=20)               # GT, MT, ROI, ECOM, HORECA, CSD, REST
    state_name   = models.CharField(max_length=100)
    city         = models.CharField(max_length=120)              # ship-to city/district (uppercased)
    sales_person = models.CharField(max_length=100, blank=True, default='')
    updated_at   = models.DateTimeField(auto_now=True)
    updated_by   = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='city_owners')

    class Meta:
        unique_together = ('channel', 'state_name', 'city')
        ordering = ['channel', 'state_name', 'city']
        indexes = [models.Index(fields=['channel', 'state_name'])]

    def __str__(self):
        return f'{self.channel}/{self.state_name}/{self.city} → {self.sales_person or "—"}'


class TerritoryProductTarget(models.Model):
    """Per-product target for one (channel, state) territory and period — the native
    grain of the Person Mapping 'Set product targets' UI. Each row = one product
    (sub_group, Premium/Commodity) with its litres + realise rate. On save these roll
    up into TargetNode (channel/state totals, per segment) and MonthlyTarget (per
    product) so the dashboard reflects them."""

    PRODUCT_TYPES = [('PREMIUM', 'Premium'), ('COMMODITY', 'Commodity')]

    channel        = models.CharField(max_length=20)               # GT, MT, ROI, ECOM, HORECA, CSD, REST (= main group)
    state_name     = models.CharField(max_length=100, blank=True, default='')
    sales_person   = models.CharField(max_length=100, blank=True, default='')  # owner, stamped from the territory map
    product_type   = models.CharField(max_length=20, choices=PRODUCT_TYPES)
    sub_group      = models.CharField(max_length=100)              # CANOLA, MUSTARD…
    month          = models.IntegerField()
    year           = models.IntegerField()
    target_ltrs    = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    target_realise = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    updated_at     = models.DateTimeField(auto_now=True)
    updated_by     = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='territory_product_targets')

    class Meta:
        unique_together = ('channel', 'state_name', 'product_type', 'sub_group', 'month', 'year')
        ordering = ['channel', 'state_name', 'product_type', 'sub_group']
        indexes = [
            models.Index(fields=['year', 'month']),
            models.Index(fields=['channel', 'state_name']),
        ]

    def __str__(self):
        return f'{self.channel}/{self.state_name} {self.product_type}:{self.sub_group} {self.month}/{self.year}'


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
