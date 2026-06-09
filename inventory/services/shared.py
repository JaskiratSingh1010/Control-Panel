import datetime as _dt
import decimal
import logging
import math

from core.sap_connector import get_connection

logger = logging.getLogger(__name__)

FG_VALID = ("'OLIVE','CANOLA','MUSTARD','SEEDS','SOYABEAN','SUNFLOWER',"
            "'GHEE','BLENDED','GROUNDNUT','SPICES','RICE BRAN','HONEY','COCONUT',"
            "'FLAKES','VITAMINS','COFFEE','DRY FRUITS/NUTS','SESAME','COTTON SEED',"
            "'SLICED OLIVE','RICE','ATTA','SOYA CHUNK','TEA','DRINKS','SNACKS','PALMOLEIN',"
            "'YELLOW MUSTARD'")
PM_VALID = ("'LABEL','CARTON','TIKKI','CAPS','PET BOTTLES','POUCH','TIN','SHRINK',"
            "'HDPE BOTTLES','GLASS BOTTLES','PET JAR','TAPE','PREFORM',"
            "'THERMOCOL','POLYBAG','DRUM','STEEL JAR','COUPON'")
RM_VALID = ("'SPICES','OLIVE','CANOLA','DRY FRUITS/NUTS','SEEDS','VITAMINS','MUSTARD',"
            "'SUNFLOWER','COCONUT','SOYABEAN','BLENDED','SESAME','GHEE','COFFEE',"
            "'RICE BRAN','GROUNDNUT','VEGETABLE OIL','PALMOLEIN','RICE','COTTON SEED',"
            "'MAIZE FLOUR (MAKKA ATTA)','YELLOW MUSTARD'")

GIFT_EXCL = 'AND M."U_Sub_Group" NOT IN (\'GIFT PACK\')'


def cf(c):
    if c and c.upper() in ('FINISHED', 'RAW MATERIAL', 'PACKAGING MATERIAL'):
        return f'AND G."ItmsGrpNam"=\'{c.upper()}\''
    return 'AND G."ItmsGrpNam" IN (\'FINISHED\',\'RAW MATERIAL\',\'PACKAGING MATERIAL\')'


def tf(t):
    return f'AND M."U_TYPE"=\'{safe(t)}\'' if t and t != 'all' else ''


def wf(w):
    return f'AND W."WhsCode"=\'{safe(w)}\'' if w else ''


def safe(s):
    return str(s).replace("'", "''") if s else ''


def cv(v):
    if v is None:
        return None
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (_dt.datetime, _dt.date)):
        return v.isoformat()
    if hasattr(v, 'item'):
        try:
            return v.item()
        except Exception:
            pass
    if isinstance(v, (int, float, str, bool)):
        return v
    return str(v)


def q(sql, conn_factory=get_connection):
    conn = None
    cursor = None
    try:
        conn = conn_factory()
        cursor = conn.cursor()
        cursor.execute(sql)
        columns = [desc[0] for desc in cursor.description]
        return [
            {col: cv(val) for col, val in zip(columns, row)}
            for row in cursor.fetchall()
        ]
    except Exception:
        logger.exception('[inventory] SAP query failed')
        return []
    finally:
        if cursor:
            try:
                cursor.close()
            except Exception:
                pass
        if conn:
            try:
                conn.close()
            except Exception:
                pass
