from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from flask_socketio import SocketIO, emit
import redis
from redis import ConnectionPool
import json
import time
from datetime import datetime
import random
import os

# ==============================
# Redis 設定
# ==============================
REDIS_HOST = os.environ.get("REDIS_HOST")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
REDIS_USERNAME = os.environ.get("REDIS_USERNAME", "default")
REDIS_PASSWORD = os.environ.get("REDIS_PASSWORD", "")

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")  # 可在 Render 設環境變數覆蓋

pool = ConnectionPool(
    host=REDIS_HOST,
    port=REDIS_PORT,
    username=REDIS_USERNAME,
    password=REDIS_PASSWORD,
    decode_responses=True,
    max_connections=30,
)
r = redis.Redis(connection_pool=pool)

try:
    r.ping()
    print("✓ Connected to Redis Cloud")
except Exception as e:
    print("✗ Redis 連線失敗:", e)


# ==============================
# Flask + CORS + Socket.IO
# ==============================
app = Flask(__name__, static_folder=".", static_url_path="")
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*")


def broadcast(event, payload):
    """透過 WebSocket 廣播事件（所有連線）"""
    socketio.emit(event, payload)


def require_admin():
    """簡單的後台密碼驗證"""
    pwd = request.headers.get("X-ADMIN-PASS", "")
    if pwd != ADMIN_PASSWORD:
        return False
    return True


# ==============================
# 前端入口
# ==============================
@app.route("/")
def index():
    return send_from_directory(".", "index.html")


@app.route("/admin")
def admin_page():
    return send_from_directory(".", "admin.html")


# ==============================
# 帳號系統
# ==============================
@app.route("/api/player/login", methods=["POST"])
def player_login():
    data = request.json or {}
    username = data.get("username")
    if not username:
        return jsonify({"success": False, "message": "請輸入名稱"}), 400

    # 檢查是否已有此玩家
    for key in r.scan_iter("player:*"):
        if r.hget(key, "username") == username:
            player_id = key.split(":", 1)[1]
            r.hset(key, "last_login", datetime.now().isoformat())
            return jsonify(
                {
                    "success": True,
                    "message": "登入成功",
                    "player_id": player_id,
                }
            )

    # 建立新玩家
    player_id = f"{int(time.time() * 1000)}"
    key = f"player:{player_id}"
    player_data = {
        "username": username,
        "gold": "1000",
        "level": "1",
        "exp": "0",
        "created_at": datetime.now().isoformat(),
        "last_login": datetime.now().isoformat(),
    }
    r.hset(key, mapping=player_data)
    r.zadd("leaderboard:gold", {player_id: 1000})
    r.zadd("leaderboard:clicks", {player_id: 0})

    broadcast("player_update", {"msg": f"{username} 加入遊戲"})

    return jsonify(
        {
            "success": True,
            "message": "玩家已建立",
            "player_id": player_id,
        }
    )


@app.route("/api/player/logout", methods=["POST"])
def player_logout():
    # 前端自行清掉狀態即可
    return jsonify({"success": True, "message": "登出成功"})


@app.route("/api/player/switch", methods=["POST"])
def player_switch():
    # 簡化處理：前端直接重新 login
    return jsonify({"success": True, "message": "切換成功"})


# ==============================
# 玩家資料
# ==============================
@app.route("/api/player/<player_id>")
def get_player(player_id):
    key = f"player:{player_id}"
    if not r.exists(key):
        return jsonify({"success": False, "message": "玩家不存在"}), 404

    pdata = r.hgetall(key)
    inventory_raw = r.lrange(f"inventory:{player_id}", 0, -1)
    inventory = [json.loads(i) for i in inventory_raw]

    gold_rank = r.zrevrank("leaderboard:gold", player_id)
    click_rank = r.zrevrank("leaderboard:clicks", player_id)
    total_clicks = r.get(f"clicks:{player_id}")
    current_combo = r.get(f"combo:{player_id}")

    return jsonify(
        {
            "success": True,
            "player": {
                **pdata,
                "player_id": player_id,
                "inventory": inventory,
                "gold_rank": (gold_rank + 1) if gold_rank is not None else None,
                "click_rank": (click_rank + 1) if click_rank is not None else None,
                "total_clicks": int(total_clicks) if total_clicks else 0,
                "current_combo": int(current_combo) if current_combo else 0,
            },
        }
    )


# ==============================
# 點擊賺金幣（動態冷卻 + 滑動視窗限流）
# ==============================
@app.route("/api/click/<player_id>", methods=["POST"])
def click(player_id):
    if not r.exists(f"player:{player_id}"):
        return jsonify({"success": False, "message": "玩家不存在"}), 404

    now_ms = int(time.time() * 1000)

    # ---- 讀取設定（如果沒有就用預設值）----
    cooldown_ms_raw = r.get("config:click_cooldown_ms")  # 單次冷卻
    window_ms_raw = r.get("config:click_window_ms")  # 滑動視窗長度
    max_hits_raw = r.get("config:click_max_hits")  # 視窗內最大點數

    cooldown_ms = int(cooldown_ms_raw) if cooldown_ms_raw else 500  # 預設 500ms
    window_ms = int(window_ms_raw) if window_ms_raw else 1000  # 預設 1000ms
    max_hits = int(max_hits_raw) if max_hits_raw else 3  # 預設 3 次

    # ==========================
    # A. 滑動視窗限流 (Rate Limit)
    # ==========================
    rate_key = f"rate:clicks:{player_id}"

    # 移除視窗之外（太舊）的紀錄
    r.zremrangebyscore(rate_key, 0, now_ms - window_ms)

    # 計算視窗內剩多少點擊
    current_hits = r.zcard(rate_key)
    if current_hits >= max_hits:
        oldest = r.zrange(rate_key, 0, 0, withscores=True)
        retry_after_ms = 0
        if oldest:
            _, oldest_ts = oldest[0]
            retry_after_ms = max(0, int(oldest_ts + window_ms - now_ms))

        return (
            jsonify(
                {
                    "success": False,
                    "message": "點擊過於頻繁（觸發滑動視窗限流）",
                    "retry_after_ms": retry_after_ms,
                    "limit_window_ms": window_ms,
                    "limit_max_hits": max_hits,
                }
            ),
            429,
        )

    # 這次點擊允許，加入視窗紀錄
    r.zadd(rate_key, {str(now_ms): now_ms})

    # ==========================
    # B. 單次冷卻時間（動態冷卻）
    # ==========================
    cooldown_key = f"cooldown:{player_id}"

    if r.exists(cooldown_key):
        ttl_ms = r.pttl(cooldown_key)
        return (
            jsonify(
                {
                    "success": False,
                    "message": "冷卻中",
                    "cooldown_ms": ttl_ms if ttl_ms > 0 else cooldown_ms,
                }
            ),
            429,
        )

    if cooldown_ms > 0:
        r.psetex(cooldown_key, cooldown_ms, 1)

    # ==========================
    # C. 計算獎勵
    # ==========================
    combo_key = f"combo:{player_id}"
    combo = int(r.get(combo_key)) if r.get(combo_key) else 0

    base_reward = 10
    combo_bonus = min(combo * 2, 50)
    is_critical = random.random() < 0.1
    total_reward = base_reward + combo_bonus
    if is_critical:
        total_reward *= 2

    lua = r.register_script(
        """
    local p = KEYS[1]
    local clicks_key = KEYS[2]
    local lb_gold = KEYS[3]
    local lb_clicks = KEYS[4]
    local pid = ARGV[1]
    local reward = tonumber(ARGV[2])

    local new_gold = redis.call("HINCRBY", p, "gold", reward)
    local new_clicks = redis.call("INCR", clicks_key)

    redis.call("ZADD", lb_gold, new_gold, pid)
    redis.call("ZADD", lb_clicks, new_clicks, pid)

    return {new_gold, new_clicks}
    """
    )

    new_gold, new_clicks = lua(
        keys=[
            f"player:{player_id}",
            f"clicks:{player_id}",
            "leaderboard:gold",
            "leaderboard:clicks",
        ],
        args=[player_id, total_reward],
    )

    # 連擊有效時間 10 秒
    r.setex(combo_key, 10, combo + 1)

    # 寫入 Stream 做歷史紀錄
    try:
        r.xadd(
            "stream:clicks",
            {
                "player_id": player_id,
                "reward": total_reward,
                "combo": combo + 1,
                "critical": "1" if is_critical else "0",
                "timestamp": datetime.now().isoformat(),
            },
            maxlen=1000,
        )
    except Exception as e:
        print("STREAM CLICK ERROR:", e)

    broadcast("leaderboard_update", {})

    return jsonify(
        {
            "success": True,
            "reward": total_reward,
            "gold": int(new_gold),
            "combo": combo + 1,
            "critical": is_critical,
            "total_clicks": int(new_clicks),
            "cooldown_ms": cooldown_ms,
            "rate_limit_window_ms": window_ms,
            "rate_limit_max_hits": max_hits,
        }
    )


# ==============================
# 商店系統
# ==============================
@app.route("/api/shop/items")
def shop_items():
    items = []
    for key in r.scan_iter("item:*"):
        sub = key[5:]  # 去掉 "item:"
        if ":" in sub:
            continue
        item_id = sub
        data = r.hgetall(key)
        stock = r.get(f"stock:{item_id}")

        parsed = {}
        for k, v in data.items():
            parsed[k] = int(v) if isinstance(v, str) and v.isdigit() else v

        items.append(
            {
                "id": item_id,
                **parsed,
                "stock": int(stock) if stock else 0,
            }
        )
    return jsonify({"success": True, "items": items})


@app.route("/api/shop/buy", methods=["POST"])
def shop_buy():
    """購買物品"""
    data = request.json or {}
    player_id = data.get("player_id")
    item_id = data.get("item_id")
    quantity = int(data.get("quantity", 1))

    if not r.exists(f"item:{item_id}"):
        return jsonify({"success": False, "message": "商品不存在"}), 404

    lua_script = """
    local player_key = KEYS[1]
    local stock_key = KEYS[2]
    local item_key = KEYS[3]
    local lb_key = KEYS[4]
    local player_id = ARGV[1]
    local quantity = tonumber(ARGV[2])

    local stock = tonumber(redis.call('GET', stock_key))
    if not stock or stock < quantity then
        return {0, "庫存不足"}
    end

    local price = tonumber(redis.call('HGET', item_key, 'price'))
    local player_gold = tonumber(redis.call('HGET', player_key, 'gold'))
    local total_cost = price * quantity

    if player_gold < total_cost then
        return {0, "金幣不足"}
    end

    local new_gold = redis.call('HINCRBY', player_key, 'gold', -total_cost)
    redis.call('DECRBY', stock_key, quantity)
    redis.call('ZADD', lb_key, new_gold, player_id)

    return {1, "購買成功", total_cost, new_gold}
    """

    script = r.register_script(lua_script)
    result = script(
        keys=[
            f"player:{player_id}",
            f"stock:{item_id}",
            f"item:{item_id}",
            "leaderboard:gold",
        ],
        args=[player_id, quantity],
    )

    if int(result[0]) != 1:
        return jsonify({"success": False, "message": result[1]}), 400

    item_data = r.hgetall(f"item:{item_id}")
    for _ in range(quantity):
        inventory_item = {
            "item_id": item_id,
            "name": item_data["name"],
            "unique_id": time.time() * 1000 + random.random(),
            "acquired_at": datetime.now().isoformat(),
        }
        r.rpush(f"inventory:{player_id}", json.dumps(inventory_item))

    broadcast("leaderboard_update", {})

    return jsonify(
        {
            "success": True,
            "message": f"購買成功！花費 {result[2]} 金幣",
            "gold": int(result[3]),  # ✅ 回傳最新金幣
        }
    )


# ==============================
# 拍賣系統
# ==============================
@app.route("/api/auction/create", methods=["POST"])
def auction_create():
    data = request.json or {}
    pid = data.get("player_id")
    uid = data.get("unique_id")
    starting_price = int(data.get("starting_price", 0))

    inv = r.lrange(f"inventory:{pid}", 0, -1)
    item = None
    idx = -1
    for i, row in enumerate(inv):
        d = json.loads(row)
        if d.get("unique_id") == uid:
            item = d
            idx = i
            break

    if not item:
        return jsonify({"success": False, "message": "物品不存在"}), 404

    # 從背包移除
    r.lset(f"inventory:{pid}", idx, "__DEL__")
    r.lrem(f"inventory:{pid}", 1, "__DEL__")

    auction_id = f"auction:{int(time.time() * 1000)}"
    seller_name = r.hget(f"player:{pid}", "username")

    auction_data = {
        "seller": pid,
        "seller_name": seller_name,
        "item_id": item["item_id"],
        "item_name": item["name"],
        "current_price": starting_price,
        "highest_bidder": "",
        "highest_bidder_name": "",
        "created_at": datetime.now().isoformat(),
    }

    r.hset(auction_id, mapping=auction_data)
    r.zadd("auctions:active", {auction_id: time.time()})

    broadcast(
        "auction_update",
        {"type": "create", "auction": {**auction_data, "id": auction_id}},
    )

    return jsonify({"success": True, "auction_id": auction_id})


@app.route("/api/auction/list")
def auction_list():
    ids = r.zrange("auctions:active", 0, -1)
    auctions = []
    for aid in ids:
        if not r.exists(aid):
            continue
        data = r.hgetall(aid)
        data["current_price"] = int(data["current_price"])
        auctions.append({"id": aid, **data})
    return jsonify({"success": True, "auctions": auctions})


@app.route("/api/auction/bid", methods=["POST"])
def auction_bid():
    data = request.json or {}
    aid = data.get("auction_id")
    pid = data.get("player_id")
    bid_amount = int(data.get("bid_amount", 0))

    if not r.exists(aid):
        return jsonify({"success": False, "message": "拍賣不存在"}), 404

    auction_data = r.hgetall(aid)
    current_price = int(auction_data["current_price"])

    if bid_amount <= current_price:
        return jsonify({"success": False, "message": "出價必須高於目前價格"}), 400

    player_gold = int(r.hget(f"player:{pid}", "gold"))
    if player_gold < bid_amount:
        return jsonify({"success": False, "message": "金幣不足"}), 400

    # 退回前一個得標者金幣 & 更新排行榜
    prev_bidder = auction_data.get("highest_bidder")
    if prev_bidder:
        new_prev_gold = r.hincrby(f"player:{prev_bidder}", "gold", current_price)
        r.zadd("leaderboard:gold", {prev_bidder: new_prev_gold})

    # 扣除目前出價者金幣 & 更新排行榜
    new_gold = r.hincrby(f"player:{pid}", "gold", -bid_amount)
    r.zadd("leaderboard:gold", {pid: new_gold})

    username = r.hget(f"player:{pid}", "username")
    r.hset(
        aid,
        mapping={
            "current_price": bid_amount,
            "highest_bidder": pid,
            "highest_bidder_name": username,
        },
    )

    # 寫入出價紀錄
    r.xadd(
        "stream:auction:bids",
        {
            "auction_id": aid,
            "bidder": pid,
            "amount": bid_amount,
            "timestamp": datetime.now().isoformat(),
        },
        maxlen=1000,
    )

    broadcast(
        "auction_update",
        {
            "type": "bid",
            "auction_id": aid,
            "bidder": pid,
            "bidder_name": username,
            "amount": bid_amount,
        },
    )
    broadcast("leaderboard_update", {})

    return jsonify(
        {
            "success": True,
            "message": "出價成功",
            "gold": int(new_gold),  # ✅ 回傳最新金幣
        }
    )


@app.route("/api/auction/buy/<auction_id>", methods=["POST"])
def auction_buy(auction_id):
    """
    直接購買成交：
    - 不重複加 'auction:' 前綴
    - 幫賣家加金幣
    - 同步更新金幣排行榜
    """
    data = request.get_json() or {}
    buyer_id = data.get("player_id")
    if not buyer_id:
        return jsonify({"success": False, "message": "缺少 player_id"}), 400

    # 👉 正確處理 key：如果已經是 "auction:1234" 就直接用，不要再加前綴
    if auction_id.startswith("auction:"):
        auction_key = auction_id
    else:
        auction_key = f"auction:{auction_id}"

    # 讀取拍賣資料
    if not r.exists(auction_key):
        return jsonify({"success": False, "message": "拍賣不存在"}), 404

    auction_data = r.hgetall(auction_key)
    seller_id = auction_data["seller"]
    price = int(auction_data["current_price"])
    item_name = auction_data["item_name"]
    item_id = auction_data["item_id"]

    # 檢查買家金幣
    buyer_key = f"player:{buyer_id}"
    seller_key = f"player:{seller_id}"

    buyer_gold = int(r.hget(buyer_key, "gold") or 0)
    if buyer_gold < price:
        return jsonify({"success": False, "message": "金幣不足"}), 400

    # 1. 扣買家金幣
    new_buyer_gold = r.hincrby(buyer_key, "gold", -price)
    # 2. 給賣家金幣
    new_seller_gold = r.hincrby(seller_key, "gold", price)

    # 2-1. 同步更新金幣排行榜
    r.zadd("leaderboard:gold", {
        buyer_id: new_buyer_gold,
        seller_id: new_seller_gold,
    })

    # 3. 給買家物品
    inventory_item = {
        "item_id": item_id,
        "name": item_name,
        "unique_id": time.time() * 1000 + random.random(),
        "acquired_at": datetime.now().isoformat(),
    }
    r.rpush(f"inventory:{buyer_id}", json.dumps(inventory_item))

    # 4. 刪除拍賣紀錄
    r.delete(auction_key)
    r.zrem("auctions:active", auction_key)

    # 5. 廣播更新
    broadcast("auction_update", {"type": "buy", "auction_id": auction_key})
    broadcast("leaderboard_update", {})  # 金幣有變化

    return jsonify({
        "success": True,
        "message": f"成功購買：{item_name}（花費 {price} 金幣）",
        "buyer_gold": int(new_buyer_gold),
        "seller_gold": int(new_seller_gold),
    })


# ==============================
# 排行榜
# ==============================
@app.route("/api/leaderboard/<board_type>")
def get_leaderboard(board_type):
    limit = int(request.args.get("limit", 10))
    key = f"leaderboard:{board_type}"
    top = r.zrevrange(key, 0, limit - 1, withscores=True)

    leaderboard = []
    for rank, (pid, score) in enumerate(top, start=1):
        username = r.hget(f"player:{pid}", "username")
        leaderboard.append(
            {
                "rank": rank,
                "player_id": pid,
                "username": username,
                "score": int(score),
            }
        )

    return jsonify({"success": True, "leaderboard": leaderboard})


# ==============================
# WebSocket 事件
# ==============================
@socketio.on("connect")
def on_connect():
    emit("server_msg", {"msg": "已連接伺服器"})


# ==============================
# 管理後台：調整冷卻 / 限流
# ==============================
@app.route("/admin/config", methods=["GET"])
def admin_get_config():
    if not require_admin():
        return jsonify({"success": False, "message": "未授權"}), 401

    cooldown_ms_raw = r.get("config:click_cooldown_ms")
    window_ms_raw = r.get("config:click_window_ms")
    max_hits_raw = r.get("config:click_max_hits")

    cooldown_ms = int(cooldown_ms_raw) if cooldown_ms_raw else 500
    window_ms = int(window_ms_raw) if window_ms_raw else 1000
    max_hits = int(max_hits_raw) if max_hits_raw else 3

    return jsonify(
        {
            "success": True,
            "cooldown_ms": cooldown_ms,
            "window_ms": window_ms,
            "max_hits": max_hits,
        }
    )


@app.route("/admin/set_cooldown", methods=["POST"])
def admin_set_cooldown():
    if not require_admin():
        return jsonify({"success": False, "message": "未授權"}), 401

    data = request.json or {}
    cooldown_ms = int(data.get("cooldown_ms", 500))
    if cooldown_ms < 0:
        cooldown_ms = 0
    r.set("config:click_cooldown_ms", cooldown_ms)

    # 讀出目前其他兩個值，讓前端一起更新
    window_ms_raw = r.get("config:click_window_ms")
    max_hits_raw = r.get("config:click_max_hits")
    window_ms = int(window_ms_raw) if window_ms_raw else 1000
    max_hits = int(max_hits_raw) if max_hits_raw else 3

    # ✅ 廣播給所有遊戲頁面：設定被修改
    broadcast(
        "config_update",
        {
            "cooldown_ms": cooldown_ms,
            "window_ms": window_ms,
            "max_hits": max_hits,
        },
    )

    return jsonify({"success": True, "cooldown_ms": cooldown_ms})


@app.route("/admin/set_rate_limit", methods=["POST"])
def admin_set_rate_limit():
    if not require_admin():
        return jsonify({"success": False, "message": "未授權"}), 401

    data = request.json or {}
    window_ms = int(data.get("window_ms", 1000))
    max_hits = int(data.get("max_hits", 3))

    if window_ms <= 0:
        window_ms = 1000
    if max_hits <= 0:
        max_hits = 1

    r.set("config:click_window_ms", window_ms)
    r.set("config:click_max_hits", max_hits)

    cooldown_ms_raw = r.get("config:click_cooldown_ms")
    cooldown_ms = int(cooldown_ms_raw) if cooldown_ms_raw else 500

    # ✅ 廣播給所有遊戲頁面
    broadcast(
        "config_update",
        {
            "cooldown_ms": cooldown_ms,
            "window_ms": window_ms,
            "max_hits": max_hits,
        },
    )

    return jsonify(
        {
            "success": True,
            "window_ms": window_ms,
            "max_hits": max_hits,
        }
    )


# ==============================
# 初始化遊戲資料
# ==============================
def init_game_data():
    items = {
        "sword_bronze": {"name": "青銅劍", "price": "100", "damage": "10"},
        "sword_silver": {"name": "白銀劍", "price": "500", "damage": "30"},
        "sword_gold": {"name": "黃金劍", "price": "2000", "damage": "80"},
        "armor_leather": {"name": "皮甲", "price": "150", "defense": "15"},
        "armor_iron": {"name": "鐵甲", "price": "600", "defense": "40"},
        "potion_health": {"name": "生命藥水", "price": "50", "heal": "100"},
    }
    for item_id, data in items.items():
        key = f"item:{item_id}"
        if not r.exists(key):
            r.hset(key, mapping=data)
        stock_key = f"stock:{item_id}"
        if not r.exists(stock_key):
            r.set(stock_key, 100)
    print("✓ 遊戲物品初始化完成")


if __name__ == "__main__":
    print("==== GAME SERVER START ====")
    init_game_data()
    port = int(os.environ.get("PORT", 5000))
    socketio.run(
        app,
        host="0.0.0.0",
        port=port,
        allow_unsafe_werkzeug=True,
    )
