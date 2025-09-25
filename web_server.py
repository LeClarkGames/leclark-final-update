from quart import Quart, request, render_template, abort, websocket, flash, redirect, url_for, jsonify, make_response
import discord
import os
import httpx
import aiosqlite
from dotenv import load_dotenv
import asyncio
import logging
import json
import secrets
from collections import defaultdict

import database
from cogs.ranking import get_rank_info 

load_dotenv()

app = Quart(__name__)
log = logging.getLogger(__name__)

user_cache = {}
cache_lock = asyncio.Lock()
CACHE_DURATION_SECONDS = 300 # Cache users for 5 minutes

# --- CONFIGURATION & GLOBALS ---
APP_BASE_URL = os.getenv("APP_BASE_URL", "http://127.0.0.1:5000")
TWITCH_CLIENT_ID = os.getenv("TWITCH_CLIENT_ID")
TWITCH_CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET")
YOUTUBE_CLIENT_ID = os.getenv("YOUTUBE_CLIENT_ID")
YOUTUBE_CLIENT_SECRET = os.getenv("YOUTUBE_CLIENT_SECRET")
DB_FILE = "bot_database.db"

TWITCH_REDIRECT_URI = f"{APP_BASE_URL}/callback/twitch"
YOUTUBE_REDIRECT_URI = f"{APP_BASE_URL}/callback/youtube"

# --- WebSocket Connection Manager ---
class WebSocketManager:
    def __init__(self):
        self.active_connections: dict[int, set] = defaultdict(set)
        log.info("WebSocketManager initialized.")

    async def register(self, guild_id: int, ws_conn):
        self.active_connections[guild_id].add(ws_conn)
        log.info(f"New WebSocket connection registered for Guild ID: {guild_id}. Total: {len(self.active_connections[guild_id])}")

    async def unregister(self, guild_id: int, ws_conn):
        if ws_conn in self.active_connections[guild_id]:
            self.active_connections[guild_id].remove(ws_conn)
            log.info(f"WebSocket connection unregistered for Guild ID: {guild_id}. Remaining: {len(self.active_connections[guild_id])}")

    async def broadcast(self, guild_id: int, message: dict):
        if guild_id in self.active_connections:
            message_json = json.dumps(message)
            connections = list(self.active_connections[guild_id])
            for ws_conn in connections:
                try:
                    await ws_conn.send(message_json)
                except Exception:
                    pass
    
ws_manager = WebSocketManager()
app.ws_manager = ws_manager

# --- HELPER FUNCTIONS ---
async def get_verification_data(state: str):
    try:
        async with aiosqlite.connect(DB_FILE) as conn:
            async with conn.execute("SELECT server_name, bot_avatar_url FROM verification_links WHERE state = ?", (state,)) as cursor:
                data = await cursor.fetchone()
                if data: return {"server_name": data[0], "bot_avatar_url": data[1]}
    except Exception as e:
        print(f"Error fetching verification data: {e}")
    return {"server_name": "your Discord server", "bot_avatar_url": ""}

async def fetch_user_data(user_id: int):
    """Fetches user data from Discord API with caching."""
    async with cache_lock:
        current_time = asyncio.get_event_loop().time()
        if user_id in user_cache and (current_time - user_cache[user_id]['timestamp']) < CACHE_DURATION_SECONDS:
            return user_cache[user_id]['data']

    bot = app.bot_instance
    try:
        user = bot.get_user(user_id) or await bot.fetch_user(user_id)
        if user:
            data = {"name": user.display_name, "avatar_url": user.display_avatar.url}
            async with cache_lock:
                user_cache[user_id] = {
                    'data': data,
                    'timestamp': asyncio.get_event_loop().time()
                }
            return data
    except Exception as e:
        log.warning(f"Could not fetch user data for {user_id}: {e}")

    return {"name": "Unknown User", "avatar_url": "https://cdn.discordapp.com/embed/avatars/0.png"}
    
async def is_valid_staff(guild_id, approver_name):
    # This is a placeholder for a real staff check. 
    # In a real scenario, you'd want a more secure way to verify the approver,
    # maybe by having them log in via Discord OAuth on the web page.
    # For now, we'll just check if the name is not empty.
    return approver_name is not None and approver_name != ""

# web_server.py

async def get_full_widget_data(guild_id: int) -> dict:
    bot = app.bot_instance
    guild = bot.get_guild(guild_id)
    if not guild: return {}

    # --- Step 1: Gather base data and server status ---
    status = await database.get_setting(guild_id, 'submission_status')
    regular_queue_count = await database.get_submission_queue_count(guild_id, 'regular')
    koth_queue_count = await database.get_submission_queue_count(guild_id, 'koth')
    reviewing_user_id = await database.get_current_review(guild_id, 'regular')
    king_id = await database.get_setting(guild_id, 'koth_king_id')

    # --- Step 2: Conditionally select the KOTH leaderboard data ---
    raw_koth_lb_data = []
    koth_leaderboard_title = "Leaderboard (All-Time)"
    
    # If a battle is open, try to get the live session data
    if status == 'koth_open':
        cog = bot.get_cog("Submissions")
        if cog: # Check if the cog is loaded
            session_stats = cog.current_koth_session.get(guild_id, {})
            if session_stats:
                koth_leaderboard_title = "Leaderboard (Current Battle)"
                sorted_session = sorted(session_stats.items(), key=lambda item: item[1]['points'], reverse=True)
                # Format session data as a list of (user_id, points) tuples
                raw_koth_lb_data = [(uid, stats['points']) for uid, stats in sorted_session]

    # If no session data was found (or battle isn't open), fall back to all-time leaderboard
    if not raw_koth_lb_data:
        all_time_data = await database.get_koth_leaderboard(guild_id)
        # Format all-time data as a list of (user_id, points) tuples
        raw_koth_lb_data = [(uid, pts) for uid, pts, _, _, _ in all_time_data]

    # --- Step 3: Collect all user IDs that need to be fetched ---
    user_ids_to_fetch = set()
    if reviewing_user_id: user_ids_to_fetch.add(reviewing_user_id)
    if king_id: user_ids_to_fetch.add(king_id)
    for user_id, _ in raw_koth_lb_data[:5]: # Get top 5 users from the selected leaderboard
        user_ids_to_fetch.add(user_id)

    # --- Step 4: Fetch all user data concurrently ---
    user_fetch_tasks = [fetch_user_data(uid) for uid in user_ids_to_fetch]
    fetched_users_list = await asyncio.gather(*user_fetch_tasks)
    
    user_data_map = {uid: data for uid, data in zip(user_ids_to_fetch, fetched_users_list)}
    default_user = {"name": "None", "avatar_url": ""}

    # --- Step 5: Build the final data structure ---
    reviewing_user_name = user_data_map.get(reviewing_user_id, default_user)['name']
    king_name = user_data_map.get(king_id, default_user)['name']
    
    koth_leaderboard = [
        {"name": user_data_map.get(uid, default_user)['name'], "points": pts}
        for uid, pts in raw_koth_lb_data[:5]
    ]

    return {
        "type": "full_update",
        "regular_data": {
            "queue": regular_queue_count,
            "reviewing": reviewing_user_name
        },
        "koth_data": {
            "queue": koth_queue_count,
            "king": king_name,
            "leaderboard": koth_leaderboard,
            "leaderboard_title": koth_leaderboard_title # NEW: Pass the title to the widget
        }
    }

# --- WEB ROUTES ---
@app.route('/')
async def home():
    return "Web server for LeClark Bot is active."

@app.route('/leaderboard/<int:guild_id>')
async def xp_leaderboard(guild_id: int):
    bot = app.bot_instance; guild = bot.get_guild(guild_id)
    if not guild: return await render_template("leaderboard.html", title="Error", guild_name="Unknown Server", users=[])
    raw_leaderboard = await database.get_leaderboard(guild_id, limit=100)
    user_ids = [user_id for user_id, xp in raw_leaderboard] # Get all user IDs
    cosmetics_task = database.get_all_user_cosmetics(guild_id, user_ids)
    user_data_task = asyncio.gather(*[fetch_user_data(uid) for uid in user_ids])
    cosmetics, fetched_users = await asyncio.gather(cosmetics_task, user_data_task)
    users = []
    users = []
    for i, (user_id, xp) in enumerate(raw_leaderboard):
        user_info, (rank_name, _, _) = fetched_users[i], get_rank_info(xp)
        users.append({
            "name": user_info['name'],
            "avatar_url": user_info['avatar_url'],
            "score": xp,
            "details": f"Level: {rank_name}",
            "emoji": cosmetics.get(user_id)
        })
    return await render_template("leaderboard.html", title=f"XP Leaderboard - {guild.name}", guild_name=guild.name, guild_icon_url=guild.icon.url if guild.icon else None, users=users, score_name="XP")

@app.route('/koth/<int:guild_id>')
async def koth_leaderboard(guild_id: int):
    bot = app.bot_instance; guild = bot.get_guild(guild_id)
    if not guild: return await render_template("leaderboard.html", title="Error", guild_name="Unknown Server", users=[])
    raw_leaderboard = await database.get_koth_leaderboard(guild_id)
    user_ids = [user_id for user_id, points, w, l, s in raw_leaderboard]
    cosmetics_task = database.get_all_user_cosmetics(guild_id, user_ids)
    user_data_task = asyncio.gather(*[fetch_user_data(uid) for uid in user_ids])
    cosmetics, fetched_users = await asyncio.gather(cosmetics_task, user_data_task)
    
    users = []
    for i, (user_id, points, wins, losses, streak) in enumerate(raw_leaderboard):
        user_info = fetched_users[i]
        users.append({
            "name": user_info['name'],
            "avatar_url": user_info['avatar_url'],
            "score": points,
            "details": f"W/L: {wins}/{losses} | Streak: {streak}",
            "emoji": cosmetics.get(user_id) # Add the emoji
        })
    return await render_template("leaderboard.html", title=f"KOTH Leaderboard - {guild.name}", guild_name=guild.name, guild_icon_url=guild.icon.url if guild.icon else None, users=users, score_name="Points")

@app.route('/widget/<int:guild_id>')
async def widget_link_page(guild_id: int):
    token = await database.get_or_create_widget_token(guild_id)
    widget_url_base = f"{APP_BASE_URL}/widget/view/{token}"
    return await render_template("widget_link.html", widget_url_base=widget_url_base, guild_id=guild_id)

@app.route('/widget/view/<token>')
async def view_widget(token: str):
    guild_id = await database.get_guild_from_token(token)
    if not guild_id:
        return "<h1>Invalid or expired token. Please regenerate your link.</h1>", 403
    return await render_template("widget.html", token=token)

@app.websocket('/ws')
async def websocket_endpoint():
    ws_conn = websocket._get_current_object()
    token = websocket.args.get('token')
    if not token:
        await ws_conn.close(1008, "Token is required"); return

    guild_id = await database.get_guild_from_token(token)
    if not guild_id:
        await ws_conn.close(1008, "Invalid token"); return

    await ws_manager.register(guild_id, ws_conn)
    try:
        initial_data = await get_full_widget_data(guild_id)
        await ws_conn.send(json.dumps(initial_data))
        while True:
            await ws_conn.receive()
    except asyncio.CancelledError:
        log.info(f"WebSocket task for guild {guild_id} cancelled.")
    finally:
        await ws_manager.unregister(guild_id, ws_conn)

@app.route('/callback/twitch')
async def callback_twitch():
    auth_code, state = request.args.get('code'), request.args.get('state')
    if not auth_code or not state: return "Error: Missing authorization code or state.", 400
    token_url = "https://id.twitch.tv/oauth2/token"
    token_params = {"client_id": TWITCH_CLIENT_ID, "client_secret": TWITCH_CLIENT_SECRET, "code": auth_code, "grant_type": "authorization_code", "redirect_uri": TWITCH_REDIRECT_URI}
    async with httpx.AsyncClient() as client: response = await client.post(token_url, params=token_params)
    token_data = response.json()
    if 'access_token' not in token_data: return "Error: Could not retrieve access token from Twitch.", 400
    access_token = token_data['access_token']
    user_url = "https://api.twitch.tv/helix/users"
    headers = {"Authorization": f"Bearer {access_token}", "Client-Id": TWITCH_CLIENT_ID}
    async with httpx.AsyncClient() as client: user_response = await client.get(user_url, headers=headers)
    user_data = user_response.json()
    if not user_data.get('data'): return "Error: Could not retrieve user data from Twitch.", 400
    account_name = user_data['data'][0]['login']
    try:
        template_data = await get_verification_data(state)
        async with aiosqlite.connect(DB_FILE) as conn:
            await conn.execute("UPDATE verification_links SET status = 'verified', verified_account = ? WHERE state = ? AND status = 'pending'", (account_name, state))
            await conn.commit()
        return await render_template("success.html", account_name=account_name, **template_data)
    except Exception as e:
        print(f"Database error during Twitch callback: {e}"); return "An internal server error occurred.", 500

@app.route('/callback/youtube')
async def callback_youtube():
    auth_code, state = request.args.get('code'), request.args.get('state')
    if not auth_code or not state: return "Error: Missing authorization code or state.", 400
    token_url = "https://oauth2.googleapis.com/token"
    token_params = {"client_id": YOUTUBE_CLIENT_ID, "client_secret": YOUTUBE_CLIENT_SECRET, "code": auth_code, "grant_type": "authorization_code", "redirect_uri": YOUTUBE_REDIRECT_URI}
    async with httpx.AsyncClient() as client: response = await client.post(token_url, data=token_params)
    token_data = response.json()
    if 'access_token' not in token_data: return "Error: Could not retrieve access token from Google.", 400
    access_token = token_data['access_token']
    user_url = "https://www.googleapis.com/oauth2/v2/userinfo"
    headers = {"Authorization": f"Bearer {access_token}"}
    async with httpx.AsyncClient() as client: user_response = await client.get(user_url, headers=headers)
    user_data = user_response.json()
    if 'name' not in user_data: return "Error: Could not retrieve user data from Google.", 400
    account_name = user_data['name']
    try:
        template_data = await get_verification_data(state)
        async with aiosqlite.connect(DB_FILE) as conn:
            await conn.execute("UPDATE verification_links SET status = 'verified', verified_account = ? WHERE state = ? AND status = 'pending'", (account_name, state))
            await conn.commit()
        return await render_template("success.html", account_name=account_name, **template_data)
    except Exception as e:
        print(f"Database error during YouTube callback: {e}"); return "An internal server error occurred.", 500
    
@app.route('/user_activity/<int:guild_id>/<int:user_id>')
async def user_activity_page(guild_id: int, user_id: int):
    token = request.args.get('token')
    approver_name = "Staff Member" # Placeholder - see comment in is_valid_staff
    
    request_data = await database.get_tier_request_by_token(token)
    if not request_data or request_data['guild_id'] != guild_id or request_data['user_id'] != user_id:
        return "<h1>Invalid or expired link.</h1>", 403

    user_data = await fetch_user_data(user_id)
    activity_data = await database.get_user_activity(guild_id, user_id)
    requirements = (await database.get_all_tier_requirements(guild_id)).get(request_data['next_tier'], {})

    return await render_template(
        "user_activity.html",
        user=user_data,
        activity=activity_data,
        request=request_data,
        requirements=requirements,
        approver_name=approver_name
    )

@app.route('/approve_tier_up', methods=['POST'])
async def approve_tier_up():
    form = await request.form
    token = form.get('token')
    approver_name = form.get('approver_name')

    request_data = await database.get_tier_request_by_token(token)
    if not request_data:
        return "<h1>Invalid or expired request.</h1>", 403

    # In a real app, you would have a login system to get the approver's real Discord name.
    # For now, we use the placeholder from the form.
    
    # Put the approval data into the queue for the bot to process
    approval_details = {
        "guild_id": request_data['guild_id'],
        "user_id": request_data['user_id'],
        "new_tier": request_data['next_tier'],
        "message_id": request_data['message_id'],
        "approver_name": approver_name
    }
    app.bot_instance.tier_approval_queue.put_nowait(approval_details)

    # Clean up the one-time use token
    await database.delete_tier_request(token)

    template_data = await get_verification_data(token) # Re-using this for success page data
    return await render_template("success.html", account_name=f"User has been approved for Tier {request_data['next_tier']}", **template_data)

# REPLACE the entire activity_dashboard function with this one

@app.route('/dashboard/<int:guild_id>')
async def activity_dashboard(guild_id: int):
    bot = app.bot_instance
    guild = bot.get_guild(guild_id)
    if not guild:
        return "<h1>Guild not found.</h1>", 404

    # Fetch top users and channels
    top_users_raw = await database.get_top_users_overall(guild_id)
    top_text_raw = await database.get_top_text_channels(guild_id)
    top_voice_raw = await database.get_top_voice_channels(guild_id)

    top_users = []
    for user_id, msg_count, vc_sec in top_users_raw:
        user_info = await fetch_user_data(user_id)
        top_users.append({'name': user_info['name'], 'message_count': msg_count, 'voice_seconds': vc_sec})

    top_text = [{'name': (guild.get_channel(cid) or "Unknown Channel").name, 'message_count': count} for cid, count in top_text_raw]
    top_voice = [{'name': (guild.get_channel(cid) or "Unknown Channel").name, 'voice_seconds': secs} for cid, secs in top_voice_raw]

    # Render the template to a string first
    rendered_template = await render_template(
        "dashboard.html",
        guild_id=guild_id,
        guild_name=guild.name,
        guild_icon_url=guild.icon.url if guild.icon else None,
        top_users=top_users,
        top_text_channels=top_text,
        top_voice_channels=top_voice
    )
    
    response = await make_response(rendered_template)
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response

@app.route('/api/user_search/<int:guild_id>')
async def api_user_search(guild_id: int):
    query = request.args.get('query', '').lower()
    if not query:
        response = jsonify({"error": "No search query provided."})
        response.status_code = 400
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        return response

    bot = app.bot_instance
    guild = bot.get_guild(guild_id)
    if not guild:
        response = jsonify({"error": "Guild not found."})
        response.status_code = 404
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        return response

    found_member = None
    # Search by ID, Name#Discrim, or just Name
    if query.isdigit():
        found_member = guild.get_member(int(query))
    else:
        if '#' in query:
            found_member = discord.utils.get(guild.members, name=query.split('#')[0], discriminator=query.split('#')[1])
        if not found_member:
            found_member = discord.utils.find(lambda m: query in m.display_name.lower(), guild.members)

    if not found_member:
        response = jsonify({"error": "User not found in this server."})
        response.status_code = 404
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        return response

    # Fetch all the user's data
    activity = await database.get_user_activity(guild_id, found_member.id)
    channel_activity_raw = await database.get_user_channel_activity(guild_id, found_member.id)
    tier = await database.get_user_tier(guild_id, found_member.id)

    channel_activity = {}
    for cid, msgs, secs in channel_activity_raw:
        channel = guild.get_channel(cid)
        if channel:
            channel_activity[channel.name] = {'messages': msgs, 'voice_seconds': secs}

    final_response = jsonify({
        "name": found_member.display_name,
        "tier": tier or 1,
        "total_messages": activity.get('message_count', 0) if activity else 0,
        "total_voice_seconds": activity.get('voice_seconds', 0) if activity else 0,
        "channel_activity": channel_activity
    })
    final_response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    final_response.headers['Pragma'] = 'no-cache'
    final_response.headers['Expires'] = '0'
    return final_response