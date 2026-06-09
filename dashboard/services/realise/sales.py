import calendar
from datetime import date

from dashboard.sap_connector import execute_query


def _lower_keys(row):
    return {str(k).lower(): v for k, v in row.items()}


def _to_float(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _month_range(month, year):
    today = date.today()
    start = date(year, month, 1)
    if today.year == year and today.month == month:
        end = today
    else:
        end = date(year, month, calendar.monthrange(year, month)[1])
    return start.isoformat(), end.isoformat()


def get_sales_data(start_date, end_date):
    try:
        sql = 'CALL "JIVO_OIL_HANADB"."REPORT_SALES_ANALYSIS"(?, ?)'
        raw_rows = execute_query(sql, [start_date, end_date])
        rows = []
        for row in raw_rows:
            # Map the procedure output to what the rest of the function expects
            r = _lower_keys(row)
            rows.append({
                'type': r.get('u_type'),
                'sub_group': r.get('u_sub_group'),
                'litres': r.get('liter', 0),
                'revenue': r.get('linetotal', 0),
                'docdate': r.get('docdate')
            })
    except Exception as e:
        print(f'get_sales_data error: {e}')
        return None

    products = []
    total_litres = 0.0
    total_revenue = 0.0
    agg = {}
    for row in rows:
        product_type = str(row.get('type') or '').strip().upper()
        if product_type not in ('PREMIUM', 'COMMODITY'):
            continue
        litres = _to_float(row.get('litres'))
        revenue = _to_float(row.get('revenue'))
        sub_group = str(row.get('sub_group') or '').strip().upper()

        total_litres += litres
        total_revenue += revenue

        key = (product_type, sub_group)
        if key not in agg:
            agg[key] = {'litres': 0.0, 'revenue': 0.0}
        agg[key]['litres'] += litres
        agg[key]['revenue'] += revenue

    for (ptype, sgroup), vals in agg.items():
        products.append({
            'type': ptype,
            'sub_group': sgroup,
            'litres': vals['litres'],
            'revenue': vals['revenue'],
            'realise': round(vals['revenue'] / vals['litres'], 2) if vals['litres'] else 0,
        })

    if not products:
        return None

    products.sort(key=lambda p: (0 if p['type'] == 'PREMIUM' else 1, -p['litres']))
    return {
        'total_litres': total_litres,
        'total_tonnes': round(total_litres / 1000, 1),
        'total_revenue': total_revenue,
        'net_realise': round(total_revenue / total_litres, 2) if total_litres else 0,
        'products': products,
    }


def get_sales_comparison(month, year):
    current_start, current_end = _month_range(month, year)
    if month == 1:
        last_month, last_year = 12, year - 1
    else:
        last_month, last_year = month - 1, year
    last_start, last_end = _month_range(last_month, last_year)
    return (
        get_sales_data(current_start, current_end),
        get_sales_data(last_start, last_end),
    )


def get_historical_realise(start_date, end_date):
    try:
        sql = 'CALL "JIVO_OIL_HANADB"."REPORT_SALES_ANALYSIS"(?, ?)'
        raw_rows = execute_query(sql, [start_date, end_date])
        
        # We need to aggregate the procedure output in Python since we can't GROUP BY a procedure
        agg = {}
        for r in raw_rows:
            d = _lower_keys(r)
            doc_date = d.get('docdate')
            if not doc_date:
                continue
            # Format doc_date to YYYY-MM
            month_str = str(doc_date)[:7] 
            
            u_type = str(d.get('u_type') or '').strip().upper()
            sub_group = str(d.get('u_sub_group') or '').strip().upper()
            litres = float(d.get('liter') or 0)
            revenue = float(d.get('linetotal') or 0)
            
            key = (month_str, u_type, sub_group)
            if key not in agg:
                agg[key] = {'litres': 0.0, 'revenue': 0.0}
            agg[key]['litres'] += litres
            agg[key]['revenue'] += revenue
            
        rows = []
        for (month_str, u_type, sub_group), vals in agg.items():
            rows.append({
                'month': month_str,
                'type': u_type,
                'sub_group': sub_group,
                'litres': vals['litres'],
                'revenue': vals['revenue'],
                'realise': vals['revenue'] / vals['litres'] if vals['litres'] else 0
            })
            
        rows.sort(key=lambda x: (x['month'], x['type'], x['sub_group']))
        return [
            row for row in rows
            if str(row.get('type') or '').strip().upper() in ('PREMIUM', 'COMMODITY')
        ]
    except Exception as e:
        print(f'get_historical_realise error: {e}')
        return []


def get_drill_down(start_date, end_date, product_type=None, sub_group=None):
    try:
        sql = 'CALL "JIVO_OIL_HANADB"."REPORT_SALES_ANALYSIS"(?, ?)'
        raw_rows = execute_query(sql, [start_date, end_date])
        rows = []
        
        ptype = str(product_type).upper() if product_type else None
        sgroup = str(sub_group).upper() if sub_group else None
        
        for r in raw_rows:
            d = _lower_keys(r)
            u_type = str(d.get('u_type') or '').strip().upper()
            u_sub_group = str(d.get('u_sub_group') or '').strip().upper()
            
            if u_type not in ('PREMIUM', 'COMMODITY'):
                continue
                
            if ptype and u_type != ptype:
                continue
            if sgroup and u_sub_group != sgroup:
                continue
                
            litres = float(d.get('liter') or 0)
            revenue = float(d.get('linetotal') or 0)
            
            rows.append({
                'docdate': d.get('docdate'),
                'customer': d.get('cardname'),
                'itemcode': d.get('itemcode'),
                'itemname': d.get('itemname'),
                'type': u_type,
                'sub_group': u_sub_group,
                'litres': litres,
                'revenue': revenue,
                'realise': revenue / litres if litres else 0
            })
            
        rows.sort(key=lambda x: (x.get('docdate') or '', x.get('revenue') or 0), reverse=True)
        return [
            row for row in rows
            if str(row.get('type') or '').strip().upper() in ('PREMIUM', 'COMMODITY')
        ]
    except Exception as e:
        print(f'get_drill_down error: {e}')
        return []
