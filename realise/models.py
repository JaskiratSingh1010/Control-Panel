from django.conf import settings
from django.db import models
from django.utils import timezone


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


class ClosingRemark(models.Model):
    """Free-text 'delivery remark' for a party (customer) on the Required Credit Limit
    report — the one frontend-editable column. Keyed by SAP CardCode so the note follows
    the party across refreshes. Everything else on that report is read live from SAP."""

    card_code = models.CharField(max_length=50, unique=True)
    remark = models.CharField(max_length=255, blank=True, default='')
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                   on_delete=models.SET_NULL)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.card_code}: {self.remark[:40]}"


class CreditLock(models.Model):
    """A global freeze of the Required Credit Limit report's Total Outstanding and
    Required Limit columns. While active (and not past lock_until), those two columns
    render from the per-party snapshot captured at lock time instead of live SAP; the
    Payment Done column is the customer's SAP receipts since locked_at and Outstanding =
    snapshot Total Outstanding − Payment Done. At most one lock is active at a time."""

    locked_at = models.DateTimeField(default=timezone.now)
    lock_until = models.DateField()                 # inclusive last day the freeze holds
    days = models.PositiveIntegerField(default=30)  # the duration the user chose
    active = models.BooleanField(default=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                   on_delete=models.SET_NULL)

    def __str__(self):
        state = 'active' if self.active else 'cleared'
        return f"CreditLock {self.locked_at:%Y-%m-%d} → {self.lock_until} ({state})"


class CreditLockSnapshot(models.Model):
    """Frozen Total Outstanding / Required Limit for one party row, captured when a
    CreditLock is created. row_key = card_code|state|main_group (the report's row
    identity), so multi-row parties (a customer spanning states/groups) freeze per row."""

    lock = models.ForeignKey(CreditLock, related_name='snapshots', on_delete=models.CASCADE)
    row_key = models.CharField(max_length=160)
    card_code = models.CharField(max_length=50)
    outstanding = models.FloatField(default=0.0)
    required_limit = models.FloatField(default=0.0)

    class Meta:
        indexes = [models.Index(fields=['lock', 'row_key'])]

    def __str__(self):
        return f"{self.row_key}: {self.outstanding:.0f}"


class FlexTarget(models.Model):
    """A user's editable 'Flex TGT' override on the Sales Channel dashboard's drill table.
    It is a what-if alternative to the real target, kept per segment + period + drill row
    (row_key = the drill node path, e.g. '¦person=SUNNY JI'). Persisted so the entered
    value survives a page refresh; auto-saved on edit (no lock button)."""

    segment = models.CharField(max_length=20, blank=True, default='')   # '' = all, PREMIUM, COMMODITY
    year = models.IntegerField()
    month = models.IntegerField()
    row_key = models.CharField(max_length=255)
    value = models.FloatField(default=0.0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('segment', 'year', 'month', 'row_key')
        indexes = [models.Index(fields=['segment', 'year', 'month'])]

    def __str__(self):
        return f"{self.row_key} [{self.segment or 'ALL'} {self.month}/{self.year}]: {self.value:.0f}"


class AgingRemark(models.Model):
    """Editable per-document remark on the Customer Aging detail page. Keyed by customer
    (CardCode) + the journal line identity (row_key = 'TransId:Line_ID'), so a note follows
    its open document across refreshes. Free text, shared across users."""

    card_code = models.CharField(max_length=50)
    row_key = models.CharField(max_length=80)          # 'TransId:Line_ID'
    remark = models.CharField(max_length=255, blank=True, default='')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('card_code', 'row_key')
        indexes = [models.Index(fields=['card_code'])]

    def __str__(self):
        return f"{self.card_code}/{self.row_key}: {self.remark[:40]}"


class AgingRemarkLine(models.Model):
    """A single split behind one open document on the Customer Aging detail page. One
    invoice's Balance Due can be broken into reason buckets (TDS, RTV, Claim, …), each with
    its own free-text category, ₹ amount, and note. Keyed by customer (CardCode) + the same
    journal-line identity (row_key = 'TransId:Line_ID') used by AgingRemark, so a row's
    splits follow its open document across refreshes. Several lines per (card_code, row_key)."""

    card_code = models.CharField(max_length=50)
    row_key = models.CharField(max_length=80)          # 'TransId:Line_ID'
    category = models.CharField(max_length=60, blank=True, default='')   # free text: TDS, RTV, Claim…
    amount = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    remark = models.CharField(max_length=255, blank=True, default='')
    position = models.IntegerField(default=0)          # display order within the row
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['position', 'id']
        indexes = [models.Index(fields=['card_code', 'row_key'])]

    def __str__(self):
        return f"{self.card_code}/{self.row_key}: {self.category} {self.amount}"


class Claim(models.Model):
    """A manually-maintained claim register row. Unlike the other Realise reports, claims are
    NOT read from SAP — every field is entered by a reviewer and persisted here. SAP only powers
    the entry pickers: the party is chosen from the customer master (which fills party_code and
    main_group), and product/item are chosen from the item master. Amount, type, pass date and the
    hold/pass workflow are all manual (no derivation). ``claim_hold`` is a plain 'Yes'/'No' flag;
    ``claim_passed`` and ``hold_amount`` are the manual ₹ amounts approved / withheld; the report's
    'Claim Month & Year' column uses ``claim_month`` ('YYYY-MM', chosen on the form) when set, else
    falls back to the month of ``claim_date``. Drill By pivots on party_name (Customer) / product /
    item / main_group."""

    claim_date      = models.DateField()
    claim_month     = models.CharField(max_length=7, blank=True, default='')     # 'YYYY-MM' explicit claim period; blank = derive from claim_date
    party_code      = models.CharField(max_length=50, blank=True, default='')    # SAP CardCode (if picked)
    party_name      = models.CharField(max_length=200)                            # Party Name / Customer
    main_group      = models.CharField(max_length=100, blank=True, default='')    # channel, auto from the party
    product         = models.CharField(max_length=120, blank=True, default='')    # oil variety / sub-group
    item            = models.CharField(max_length=200, blank=True, default='')    # SKU / item name
    claim_type      = models.CharField(max_length=120, blank=True, default='')    # manual (Discount / FOC / …)
    ref_inv_no      = models.CharField(max_length=100, blank=True, default='')    # Ref. Invoice No (manual)
    coop_no         = models.CharField(max_length=100, blank=True, default='')    # COOP No (manual)
    claim_amount    = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    claim_pass_date = models.DateField(null=True, blank=True)
    claim_hold      = models.CharField(max_length=10, blank=True, default='')     # 'Yes' / 'No'
    claim_passed    = models.DecimalField(max_digits=16, decimal_places=2, default=0)  # Claim Passed (Manual) ₹
    hold_amount     = models.DecimalField(max_digits=16, decimal_places=2, default=0)  # Hold (Manual) ₹
    reason_of_hold  = models.CharField(max_length=255, blank=True, default='')    # Reason of Hold (Manual)
    created_by      = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                        on_delete=models.SET_NULL, related_name='claims')
    created_at      = models.DateTimeField(auto_now_add=True)
    updated_at      = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-claim_date', '-id']
        indexes = [
            models.Index(fields=['claim_date']),
            models.Index(fields=['party_code']),
        ]

    def __str__(self):
        return f"{self.claim_date} {self.party_name}: {self.claim_amount}"


class AgingDueConfig(models.Model):
    """Per-customer grace period (in days) driving the automatic NOT DUE / OVERDUE remark on
    the Customer Aging detail page. A document is NOT DUE while (aging date − posting date) is
    below ``grace_days`` — its Remarks cell is locked to 'NOT DUE' and can't be edited until
    the days change; once that many days pass the document is OVERDUE and the cell becomes
    editable again (defaulting to 'OVERDUE'). ``grace_days`` = 0 / no row = feature off, so the
    Remarks column behaves as plain free text. Keyed by SAP CardCode, shared across users."""

    card_code = models.CharField(max_length=50, unique=True)
    grace_days = models.PositiveIntegerField(default=0)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                   on_delete=models.SET_NULL)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.card_code}: {self.grace_days}d"
