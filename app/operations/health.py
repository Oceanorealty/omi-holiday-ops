from sqlalchemy import text
from fastapi.responses import JSONResponse


def database_health(engine):
    try:
        with engine.begin() as conn:
            if engine.dialect.name == 'postgresql':
                conn.execute(text("SET LOCAL statement_timeout = '3s'"))
            conn.execute(text('SELECT 1'))
        return JSONResponse({'status':'ok','database':'ready'},headers={'Cache-Control':'no-store'})
    except Exception:
        return JSONResponse({'status':'unavailable','database':'unavailable'},status_code=503,headers={'Cache-Control':'no-store'})
