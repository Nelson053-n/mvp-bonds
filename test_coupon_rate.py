from app.services.portfolio_service import portfolio_service
import asyncio

async def test():
    rows = await portfolio_service.get_table_fresh()
    bonds = [r for r in rows if r.type == 'bond']
    for b in bonds:
        print(f'{b.ticker}: coupon_rate={b.coupon_rate}')

asyncio.run(test())
