import importlib, config
importlib.reload(config)
from parser import parse_message

examples = [
('Ex1: Buy now tight sl', """Buy xauusd now tight sl

Tp 3890
Tp 3900
Tp 3930
Tp 3970
Tp 4000

Sl 3875

Won't count it as a loss if it's hits risk very tight sl enter at own risk"""),

('Ex2: Buy zone 3895-3887', """Buy xauusd 3895-3887

Tp 3900
Tp 3910
Tp 3930
Tp 3950
Tp 4000

Sl 3872"""),

('Ex3: SELL zone 3897-3900', """SELL XAUUSD 3897-3900

TP 3880 
TP 3860 
TP 3800 

SL 3907"""),

('Ex4: buy now zone 3896-3886', """buy now 3896-3886
SL 3870
TP 3899
TP 3906
TP 3916
TP 3920
TP 3950"""),

('Ex5: BUY 3885-3875', """BUY 3885-3875
SL 3860
TP 3888
TP 3895
TP 3898
TP 3905
TP 3920
TP 3950
TP 4000"""),

('Ex6: BUY 3889-3879', """BUY 3889-3879
SL 3869
TP 3892
TP 3899
TP 3902
TP 3910
TP 3925
TP 3950
TP 4000"""),

('Ex7: BUY 3905-3895', """BUY 3905-3895
SL 3880
TP 3908
TP 3915
TP 3925
TP 3930
TP 3950
TP 3970
TP 4000"""),
]

for label, ex in examples:
    res = parse_message(ex)
    print(f'=== {label} ===')
    if res:
        print(f'  Symbol={res.symbol} Dir={res.direction} Zone={res.is_zone} Market={res.is_market}')
        if res.entry_tiers:
            print(f'  Top={res.top} Mid={res.mid} Bottom={res.bottom}')
        elif res.single_price:
            print(f'  SinglePrice={res.single_price}')
        print(f'  TP1={res.raw_tp1} FinalTP={res.final_tp} ProxClose={res.proximity_close_price}')
        print(f'  SL={res.sl} Valid={res.sl_valid}')
        print(f'  MaxOrders={res.max_orders}')
        print(f'  PASS')
    else:
        print(f'  FAILED TO PARSE')
    print()
