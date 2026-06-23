from django.contrib import admin
from .models import (MainGroupMaster, MonthlyTarget, StateMaster, TargetMaster,
                     TerritoryMapping, TerritoryProductTarget, TargetNode, CityOwner)


@admin.register(MonthlyTarget)
class MonthlyTargetAdmin(admin.ModelAdmin):
    list_display = ('product_type', 'sub_group', 'month', 'year', 'tgt_ltrs', 'tgt_rate', 'updated_at', 'updated_by')
    list_filter = ('year', 'month', 'product_type')
    search_fields = ('sub_group',)
    ordering = ('-year', '-month', 'product_type', 'sub_group')


@admin.register(MainGroupMaster)
class MainGroupMasterAdmin(admin.ModelAdmin):
    list_display = ('name',)
    search_fields = ('name',)


@admin.register(StateMaster)
class StateMasterAdmin(admin.ModelAdmin):
    list_display = ('name',)
    search_fields = ('name',)


@admin.register(TargetMaster)
class TargetMasterAdmin(admin.ModelAdmin):
    list_display = ('main_group', 'state', 'sales_person', 'target_ltrs', 'month', 'year')
    list_filter = ('year', 'month', 'main_group')
    search_fields = ('main_group__name', 'state__name', 'sales_person')


@admin.register(TerritoryMapping)
class TerritoryMappingAdmin(admin.ModelAdmin):
    # Grid identity (channel/state) is fixed & seeded; only sales_person is editable.
    list_display = ('channel', 'state_name', 'state_code', 'sales_person', 'updated_at', 'updated_by')
    list_filter = ('channel',)
    list_editable = ('sales_person',)
    search_fields = ('channel', 'state_name', 'sales_person')
    readonly_fields = ('channel', 'state_code', 'state_name', 'updated_at', 'updated_by')


@admin.register(CityOwner)
class CityOwnerAdmin(admin.ModelAdmin):
    # City/district-level ASM override inside a (channel, state) territory.
    list_display = ('channel', 'state_name', 'city', 'sales_person', 'updated_at', 'updated_by')
    list_filter = ('channel', 'state_name')
    list_editable = ('sales_person',)
    search_fields = ('channel', 'state_name', 'city', 'sales_person')


@admin.register(TerritoryProductTarget)
class TerritoryProductTargetAdmin(admin.ModelAdmin):
    # Per-product target with its full identity: main group (channel) + state + person + product.
    list_display = ('channel', 'state_name', 'sales_person', 'product_type', 'sub_group',
                    'target_ltrs', 'target_realise', 'month', 'year', 'updated_at')
    list_filter = ('year', 'month', 'channel', 'product_type')
    search_fields = ('channel', 'state_name', 'sales_person', 'sub_group')
    ordering = ('-year', '-month', 'channel', 'state_name', 'product_type', 'sub_group')


@admin.register(TargetNode)
class TargetNodeAdmin(admin.ModelAdmin):
    # Channel+state+person roll-up (per segment) that the dashboard cards read.
    list_display = ('main_group', 'state', 'sales_person', 'segment',
                    'target_ltrs', 'target_realise', 'month', 'year')
    list_filter = ('year', 'month', 'main_group', 'segment')
    search_fields = ('main_group', 'state', 'sales_person')
    ordering = ('-year', '-month', 'main_group', 'state')
