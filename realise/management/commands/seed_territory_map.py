"""Seed / refresh the TerritoryMapping grid from live SAP data.

The grid (channel + state) is FIXED and data-driven: distinct (U_Main_Group, State1)
pairs from the SAP customer master (OCRD), mapped to the 7 dashboard channels via
``services.CHANNEL_MEMBERS``. Each new cell's ``sales_person`` is pre-filled from the
existing hardcoded TERRITORY_SHEET / CHANNEL_OWNERS where a known owner exists; the
rest start unassigned for an admin to fill in.

Usage:
    python manage.py seed_territory_map            # add any missing cells, keep edits
    python manage.py seed_territory_map --refresh  # same (explicit); never wipes persons
    python manage.py seed_territory_map --reset     # delete all rows and reseed from scratch
    python manage.py seed_territory_map --dry-run   # show what would change, write nothing
"""

from django.core.management.base import BaseCommand

from core import sap_connector
from realise import services
from realise.models import TerritoryMapping


def _raw_to_channel():
    """Raw SAP U_Main_Group -> 7-channel display name (GT/MT/ROI/ECOM/HORECA/CSD/REST)."""
    out = {}
    for channel, members in services.CHANNEL_MEMBERS.items():
        for raw in members:
            out[services._normalize_name(raw)] = channel
    return out


def _seed_persons():
    """{(channel, state_name): person} and {channel: national_owner} from the legacy sheet."""
    by_cell = {}
    for (group, _code, name, person) in services.TERRITORY_SHEET:
        by_cell[(services._normalize_name(group), services._normalize_name(name))] = \
            services._normalize_name(person)
    raw2ch = _raw_to_channel()
    by_channel = {}
    for raw_group, person in services.CHANNEL_OWNERS.items():
        ch = raw2ch.get(services._normalize_name(raw_group), services._normalize_name(raw_group))
        by_channel[ch] = services._normalize_name(person)
    return by_cell, by_channel


def _fetch_grid_cells():
    """Distinct (channel, state_code, state_name) cells from OCRD, blanks dropped."""
    sql = '''
        SELECT DISTINCT
            COALESCE(TRIM("U_Main_Group"), '') AS "G",
            COALESCE(TRIM("State1"), '')       AS "ST"
        FROM "JIVO_OIL_HANADB"."OCRD"
        WHERE COALESCE(TRIM("U_Main_Group"), '') <> ''
    '''
    rows = sap_connector.execute_query(sql)
    raw2ch = _raw_to_channel()
    cells = {}
    for r in rows:
        raw = services._normalize_name(r.get('G'))
        code = services._normalize_name(r.get('ST'))
        channel = raw2ch.get(raw)
        if channel is None:
            continue                     # not one of the 7 dashboard channels
        name = services.STATE_CODE_NAMES.get(code, code)
        if not name:
            continue                     # drop blank-state cells (not real territories)
        cells[(channel, name)] = code    # collapse raw groups sharing a channel
    return cells


class Command(BaseCommand):
    help = 'Seed/refresh the TerritoryMapping grid from live SAP (OCRD) data.'

    def add_arguments(self, parser):
        parser.add_argument('--reset', action='store_true',
                            help='Delete all rows and reseed from scratch (wipes person edits).')
        parser.add_argument('--refresh', action='store_true',
                            help='Add any new cells from SAP; never overwrite existing persons.')
        parser.add_argument('--dry-run', action='store_true',
                            help='Report changes without writing.')

    def handle(self, *args, **opts):
        dry = opts['dry_run']
        reset = opts['reset']

        self.stdout.write('Fetching channel x state grid from SAP (OCRD)…')
        try:
            cells = _fetch_grid_cells()
        except Exception as exc:
            self.stderr.write(f'SAP fetch failed: {exc}')
            return
        self.stdout.write(f'  {len(cells)} (channel, state) cells found.')

        by_cell, by_channel = _seed_persons()

        if reset and not dry:
            deleted = TerritoryMapping.objects.all().delete()[0]
            self.stdout.write(f'  --reset: deleted {deleted} existing rows.')

        existing = {(m.channel, m.state_name): m for m in TerritoryMapping.objects.all()}
        created = updated = kept = 0

        for (channel, name), code in sorted(cells.items()):
            person = by_cell.get((channel, name)) or by_channel.get(channel, '')
            current = existing.get((channel, name))
            if current is None:
                created += 1
                if not dry:
                    TerritoryMapping.objects.create(
                        channel=channel, state_code=code, state_name=name, sales_person=person)
            else:
                # Keep the cell; only backfill state_code / a still-blank person.
                changed = False
                if current.state_code != code:
                    current.state_code = code
                    changed = True
                if not current.sales_person and person:
                    current.sales_person = person
                    changed = True
                if changed and not dry:
                    current.save(update_fields=['state_code', 'sales_person', 'updated_at'])
                updated += int(changed)
                kept += int(not changed)

        # National (state-blank) owner rows for channels that resolve to one person.
        for channel, person in by_channel.items():
            if not person:
                continue
            key = (channel, '')
            if key in existing:
                continue
            if not dry:
                TerritoryMapping.objects.get_or_create(
                    channel=channel, state_name='',
                    defaults={'state_code': '', 'sales_person': person})

        if not dry:
            services.invalidate_territory_cache()

        prefix = '[dry-run] ' if dry else ''
        self.stdout.write(self.style.SUCCESS(
            f'{prefix}Done. created={created} updated={updated} unchanged={kept} '
            f'total={TerritoryMapping.objects.count()}'))
