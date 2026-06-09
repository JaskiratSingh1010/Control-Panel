from django.contrib import admin
from .models import MainGroupMaster, MonthlyTarget, StateMaster, TargetMaster


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
