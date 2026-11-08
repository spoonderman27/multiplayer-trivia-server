# Setup and import

# tools to run server and handle connections
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
import uvicorn
import json
import httpx
import random
import html
import asyncio  
import socket
import time

# Create FastAPI web server
app = FastAPI()

# Tell server where to find sound effects (ticking, correct, wrong answer)
app.mount("/sounds", StaticFiles(directory="sounds"), name="sounds")

# Game manager class
# remember who is playing, what the current score is and current questions
class GameManager:
    def __init__(self):
        # network connections
        self.host_ws: WebSocket = None       # connection to the host laptop screen
        self.players: dict = {}              # dictionary linking player connections to their names
        
        # player details
        self.scores: dict = {}               # Tracks points for each player
        self.players_avatars: dict = {}      # Remembers which emoji avatar each player chose
        
        # current Game State
        self.current_correct_answer = ""     # Holds the right answer to check if players got it right
        self.questions = []                  # list of all questions for the current game
        self.current_q_index = 0             # question number currently on
        self.answered_players = set()        # list of players who have already clicked an answer button
        
        # timers and sessions
        self.timer_duration = 10             # default question timer limit
        self.timer_task = None               # background countdown clock
        self.game_session_id = 0             # unique ID to prevent old games from interfering with new ones

# connection when player join or leave
    # function runs when host opens the website on laptop
    async def connect_host(self, websocket: WebSocket):
        await websocket.accept()
        self.host_ws = websocket
        
        # find a public web link if using ngrok for multiplayer 
        join_url = None
        try:
            import urllib.request
            req = urllib.request.urlopen("http://127.0.0.1:4040/api/tunnels", timeout=1)
            tunnels = json.loads(req.read())['tunnels']
            for t in tunnels:
                if t['proto'] == 'https':
                    join_url = f"{t['public_url']}/player"
                    break
        except Exception:
            pass

        # If playing on a local Wi-Fi network, find the computer local IP address
        if not join_url:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect(('10.255.255.255', 1))
                local_ip = s.getsockname()[0]
            except Exception:
                local_ip = '127.0.0.1'
            finally:
                s.close()
            join_url = f"http://{local_ip}:8000/player"
            
        print(f"\n=======================================================")
        print(f"📱 TO PLAY ON PHONE, GO TO: {join_url}")
        print(f"=======================================================\n")
        
        # send the link back to the host to generate qr code
        await self.send_to_host({"type": "init", "join_url": join_url})

    # when a player scans the qr code and connects, run the function
    async def connect_player(self, websocket: WebSocket):
        await websocket.accept()
        # start as blank name anonymous until a name is entered
        self.players[websocket] = "Anonymous" 

    # when a player refresh their browser or disconnected, this runs.
    def disconnect_player(self, websocket: WebSocket):
        if websocket in self.players:
            name = self.players.pop(websocket)
            
            # remove them from the active question tracking so the game dont get stuck waiting for them
            if name in getattr(self, 'answered_players', set()):
                self.answered_players.remove(name)
            if name in getattr(self, 'player_choices', {}):
                del self.player_choices[name]
                
            return name
        return "Unknown"

# messaging system
    # send direct message to host screen
    async def send_to_host(self, message: dict):
        if self.host_ws:
            try:
                await self.host_ws.send_text(json.dumps(message))
            except Exception:
                pass # If fails, ignore it to prevent the server from crashing

    # Safely send a message out to every single connected player
    async def broadcast_to_players(self, message: dict):
        # make a copy of the players list so it dont crash if someone leaves while broadcasting
        for player_ws in list(self.players.keys()):
            try:
                await player_ws.send_text(json.dumps(message))
            except Exception:
                pass

    # keeps connections alive so firewalls dont automatically disconnect players who are idle
    async def heartbeat_loop(self):
        while True:
            await asyncio.sleep(20) # Every 20 seconds...
            heartbeat_packet = {"type": "heartbeat", "timestamp": time.time()}
            await self.send_to_host(heartbeat_packet)
            await self.broadcast_to_players(heartbeat_packet)

# game logic for question and timers
    # Reaches out to the OpenTDB (open trivia database) to fetch trivia questions
    async def fetch_questions(self, amount=5, category="any"):
        print("Fetching questions from OpenTDB...")
        url = f"https://opentdb.com/api.php?amount={amount}&type=multiple"
        if category != "any":
            url += f"&category={category}"
            
        async with httpx.AsyncClient() as client:
            response = await client.get(url)
            data = response.json()
            self.questions = data["results"]
            print("Questions successfully loaded!")

    # starts the next round by displaying a new question on all screens
    async def send_current_question(self, session_id):
        # reset the answer tracking for this new round
        self.answered_players.clear()        
        self.answered_players = set()
        self.player_choices = {}
        self.question_start_time = time.time()
        
        # find out current question and the correct answer
        q = self.questions[self.current_q_index]
        self.current_correct_answer = html.unescape(q["correct_answer"])
        
        # combine the right answer with the wrong answers, then shuffle them
        all_answers = q["incorrect_answers"] + [q["correct_answer"]]
        all_answers = [html.unescape(ans) for ans in all_answers]
        random.shuffle(all_answers)
        
        # package the question data and send it out
        question_data = {
            "type": "new_question",
            "question": html.unescape(q["question"]),
            "answers": all_answers,
            "question_number": self.current_q_index + 1,
            "total_questions": len(self.questions)
        }
        
        await self.send_to_host(question_data)
        await self.broadcast_to_players(question_data)
        
        # stop any old timers that may be running and start a fresh one
        if self.timer_task:
            self.timer_task.cancel()
        self.timer_task = asyncio.create_task(self.run_timer(session_id))

    # clock that ticks down every second
    async def run_timer(self, session_id):
        try:
            # Count down from the timer duration down to 0
            for time_left in range(self.timer_duration, -1, -1):
                await self.send_to_host({"type": "timer", "time": time_left})
                await self.broadcast_to_players({"type": "timer", "time": time_left})
                await asyncio.sleep(1) # wait 1 second
            
            # when timer end, show the result and leaderboard
            if self.game_session_id == session_id:
                await self.show_leaderboard(session_id)
        except asyncio.CancelledError:
            pass # timer cancelled when a new question started
            
    # Calculates points and shows the leaderboard after a question ends
    async def show_leaderboard(self, session_id):
        # Calculate scores for every player
        for ws, name in self.players.items():
            if name == "Anonymous":
                continue # skip player without name
                
            time_taken = self.timer_duration
            # check what player pick and how fast they answer
            if name in getattr(self, 'player_choices', {}):
                choice_data = self.player_choices[name]
                choice = choice_data.get("choice")
                time_taken = choice_data.get("time_taken", self.timer_duration)
                is_correct = (choice == self.current_correct_answer)
            else:
                is_correct = False
                
            points_earned = 0
            # award points based on speed, faster answer gets more point, minimum point for late answer is half of max point)
            if is_correct:
                ratio = max(0, self.timer_duration - time_taken) / self.timer_duration
                points_earned = int(50 + (50 * ratio))
                self.scores[name] += points_earned
                
            # send the player their personal results
            try:
                await ws.send_json({
                    "type": "result",
                    "correct": is_correct,
                    "score": self.scores.get(name, 0),
                    "points_earned": points_earned,
                    "correct_answer": self.current_correct_answer,
                    "answered": name in getattr(self, 'player_choices', {})
                })
            except Exception:
                pass

        # show host the right answer
        await self.send_to_host({
            "type": "result_reveal",
            "correct_answer": self.current_correct_answer
        })
        
        # pause for 4 seconds for player to see if they get correct answer 
        await asyncio.sleep(4) 
        if self.game_session_id != session_id:
            return

        # sort the scores from highest to lowest and show the Leaderboard
        sorted_names_scores = sorted(self.scores.items(), key=lambda x: x[1], reverse=True)
        sorted_scores = [[name, score, self.players_avatars.get(name, "👤")] for name, score in sorted_names_scores]
        
        lb_packet = {"type": "leaderboard", "scores": sorted_scores}
        await self.send_to_host(lb_packet)
        await self.broadcast_to_players(lb_packet)
        
        # pause for 4 seconds for player to look at the leaderboard
        await asyncio.sleep(4) 
        if self.game_session_id != session_id:
            return
            
        # automatically move to the next question or end the game if no question left
        self.current_q_index += 1
        if self.current_q_index < len(self.questions):
            await self.send_current_question(session_id)
        else:
            await self.send_to_host({"type": "game_over", "scores": sorted_scores})
            await self.broadcast_to_players({"type": "game_over", "scores": sorted_scores})

# server startup and web page routing
# Create global game brain
game = GameManager()

# when the server first turns on, start the heartbeat loop in the background
@app.on_event("startup")
async def startup_event():
    asyncio.create_task(game.heartbeat_loop())

# when go to localhost:8000, serve the Host HTML file
@app.get("/")
async def get_host_page():
    with open("host.html", "r", encoding="utf-8") as file:
        return HTMLResponse(content=file.read())

# When you go to localhost:8000/player, serve the Player HTML file
@app.get("/player")
async def get_player_page():
    with open("player.html", "r", encoding="utf-8") as file:
        return HTMLResponse(content=file.read())

# host websocket for laptop connection 

@app.websocket("/ws/host")
async def host_endpoint(websocket: WebSocket):
    # Establish the connection
    await game.connect_host(websocket)
    try:
        # constantly listen for messages from the host browser
        while True:
            data = await websocket.receive_text()
            
            try:
                message = json.loads(data)
                
                # check latency ping and instantly bounce it back
                if message.get("action") == "ping":
                    await websocket.send_json({"type": "pong", "start": message.get("start")})
                    continue
                    
                # when host clicked the start game button
                elif message.get("action") == "START_GAME":
                    config = message.get("config", {})
                    amount = config.get("amount", 5)
                    category = config.get("category", "any")
                    game.timer_duration = config.get("timer", 15)
                    
                    game.game_session_id += 1
                    current_session = game.game_session_id
                    
                    # reset all player score to 0
                    for player_name in game.players.values():
                        if player_name != "Anonymous":
                            game.scores[player_name] = 0

                    # fetch internet questions and start round 1
                    await game.fetch_questions(amount, category)
                    game.current_q_index = 0
                    await game.send_current_question(current_session)
                    
                # host clicked "Start Game" but use custom questions
                elif message.get("action") == "START_CUSTOM_GAME":
                    config = message.get("config", {})
                    game.timer_duration = config.get("timer", 15)
                    game.questions = config.get("questions", [])
                    
                    game.game_session_id += 1
                    current_session = game.game_session_id
                    
                    # Reset scores
                    for player_name in game.players.values():
                        if player_name != "Anonymous":
                            game.scores[player_name] = 0

                    # Start round 1 using the custom questions array
                    game.current_q_index = 0
                    await game.send_current_question(current_session)
                    
            # if error occur, log to terminal to fix
            except json.JSONDecodeError as e:
                    print(f"⚠️ WARNING: Received broken JSON data from client.")
                    print(f"Data received: {data}")
                    print(f"Error details: {e}")

    # If the Host closes the browser tab, stop the game
    except WebSocketDisconnect:
        game.host_ws = None
        if game.timer_task:
            game.timer_task.cancel()
        game.game_session_id += 1
        # Tell all phones that the game was aborted
        asyncio.create_task(game.broadcast_to_players({"type": "game_aborted"}))

# player websocket for phone connection 
@app.websocket("/ws/player")
async def player_endpoint(websocket: WebSocket):
    # Establish connection
    await game.connect_player(websocket)
    try:
        # listen for messages from players phone
        while True:
            data = await websocket.receive_text() 
            message = json.loads(data) 
            
            # check latency ping and instantly bounce it back
            if message.get("action") == "ping":
                await websocket.send_json({"type": "pong", "start": message.get("start")})
                continue
                
            # when player type name and click join
            elif message["action"] == "join":
                player_name = message["name"]
                avatar = message.get("avatar", "👤")
                
                # link connection to player name and avatar
                game.players[websocket] = player_name
                game.players_avatars[player_name] = avatar
                
                # when player join, score start from 0
                # if player reconnected, they have their old score saved
                if player_name not in game.scores:
                    game.scores[player_name] = 0
                    
                # tell host screen to add player to lobby waiting list
                await game.send_to_host({"type": "join", "name": player_name, "avatar": avatar})
                # tell player phone screen it successfully joined
                await websocket.send_json({"type": "join_ack"})
                
            # when player click answer button
            elif message["action"] == "answer":
                player_name = game.players[websocket]
                
                # prevent player from answering twice
                if player_name in game.answered_players:
                    continue
                    
                game.answered_players.add(player_name)
                chosen_answer = message["choice"]
                
                # calculate how long player takes to choose answer
                time_taken = time.time() - getattr(game, 'question_start_time', time.time() - game.timer_duration)
                if not hasattr(game, 'player_choices'):
                    game.player_choices = {}
                
                # save player chosen answer and time taken
                game.player_choices[player_name] = {
                    "choice": chosen_answer,
                    "time_taken": time_taken
                }
                
                # send player chosen answer back to player's phone
                await websocket.send_text(json.dumps({
                    "type": "ack",
                    "text": f"You chose: {chosen_answer}. Waiting for timer..."
                }))
                
                # update host screen to show how many player answered, X/total count
                total_players = len([p for p in game.players.values() if p != "Anonymous"])
                await game.send_to_host({
                    "type": "player_answered", 
                    "name": player_name,
                    "answered_count": len(game.answered_players),
                    "total_count": total_players
                })
            
    # when player connection drops, from refresh or idle
    except WebSocketDisconnect:
        name = game.disconnect_player(websocket)
        # tell host to remove player from lobby
        await game.send_to_host({"type": "leave", "name": name})

# run the server and start locally on port 8000
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)