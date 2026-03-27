# 📱 WhatsApp CRM

A production-ready, multi-tenant WhatsApp CRM platform built with **FastAPI** (backend) and **React + Vite** (frontend), powered by the **Meta WhatsApp Cloud API**.

---

## ✨ Features

| Feature | Description |
|---|---|
| 💬 **Inbox** | Real-time WhatsApp conversations with WebSocket live updates |
| 📢 **Broadcasts** | Send bulk template campaigns to segmented contacts |
| 📋 **Templates** | Create, manage & sync WhatsApp message templates with Meta |
| 🤖 **Automations** | Auto-reply rules — keyword triggers, first message, outside hours |
| 👥 **Contacts** | Contact management with tags, opt-in tracking, and search |
| 📊 **Analytics** | Message delivery stats, read rates, campaign performance |
| 👤 **Agents** | Multi-agent support with conversation assignment |
| ⚙️ **Settings** | Tenant configuration, WhatsApp Business account setup |

---

## 🏗️ Tech Stack

### Backend
- **FastAPI** — async Python web framework
- **MongoDB** + **Beanie** (async ODM)
- **Meta WhatsApp Cloud API** v22.0
- **WebSockets** — real-time inbox updates
- **JWT** authentication + Google OAuth
- **httpx** — async HTTP client

### Frontend
- **React 18** + **Vite**
- **Tailwind CSS v4**
- **React Router v6**
- **Axios** — API client

---

## 📁 Project Structure

```
wcrm/
├── backend/                    # FastAPI application
│   ├── app/
│   │   ├── api/v1/             # API route handlers
│   │   │   ├── auth.py         # JWT auth, login, register
│   │   │   ├── conversations.py # Inbox messages API
│   │   │   ├── broadcasts.py   # Broadcast campaigns
│   │   │   ├── templates.py    # WhatsApp template CRUD
│   │   │   ├── autoreplies.py  # Auto-reply rules engine
│   │   │   ├── contacts.py     # Contact management
│   │   │   ├── agents.py       # Agent management
│   │   │   ├── analytics.py    # Stats & metrics
│   │   │   ├── webhook.py      # Meta webhook receiver
│   │   │   └── websocket.py    # WebSocket endpoint
│   │   ├── services/
│   │   │   └── whatsapp.py     # Meta Cloud API client
│   │   ├── models/
│   │   │   └── tenant.py       # Beanie document models
│   │   ├── core/
│   │   │   ├── dependencies.py # FastAPI dependencies
│   │   │   └── security.py     # JWT + encryption
│   │   ├── config.py           # Settings (pydantic)
│   │   ├── database.py         # MongoDB connection
│   │   └── main.py             # App entry point
│   ├── .env.example            # Environment template
│   ├── requirements.txt        # Python dependencies
│   └── .gitignore
│
└── frontend/                   # React application
    ├── src/
    │   ├── pages/
    │   │   ├── auth/           # Login, Register
    │   │   ├── dashboard/
    │   │   │   ├── Inbox.jsx           # WhatsApp Inbox
    │   │   │   ├── Broadcasts.jsx      # Broadcast campaigns
    │   │   │   ├── Templates.jsx       # Template management
    │   │   │   ├── Automations.jsx     # Auto-reply rules
    │   │   │   ├── Contacts.jsx        # Contact management
    │   │   │   ├── SendTemplateModal.jsx # Template send UI
    │   │   │   ├── Layout.jsx          # Dashboard shell
    │   │   │   └── Dashboard.jsx       # Home page
    │   │   └── onboarding/     # WhatsApp setup wizard
    │   ├── services/
    │   │   └── api.js          # Axios instance + interceptors
    │   ├── hooks/
    │   │   └── useInboxSocket.js # WebSocket hook
    │   └── App.jsx             # Routes
    ├── .env.example
    └── .gitignore
```

---

## 🚀 Quick Start

### Prerequisites
- Python 3.12+
- Node.js 18+
- MongoDB (local or Atlas)
- Meta Developer Account with WhatsApp Business API access

---

### Backend Setup

```bash
cd backend

# Create virtual environment
python3 -m venv wp_env
source wp_env/bin/activate   # Windows: wp_env\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env with your values (see Environment Variables section below)

# Start server
uvicorn app.main:app --reload --port 8002 --host 0.0.0.0
```

API docs available at: `http://localhost:8002/docs`

---

### Frontend Setup

```bash
cd frontend

# Install dependencies
npm install

# Configure environment
cp .env.example .env
# Edit VITE_API_URL if backend runs on different port

# Start dev server
npm run dev
```

App available at: `http://localhost:5173`

---

## ⚙️ Environment Variables

### Backend `.env`

```env
# App
APP_ENV=development

# Security
SECRET_KEY=your-secret-key-min-32-chars
ENCRYPTION_KEY=your-fernet-key-44-chars-base64
ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=60

# MongoDB
MONGODB_URL=mongodb://localhost:27017/
MONGODB_DB_NAME=whatsapp_business

# Meta / WhatsApp
META_APP_ID=your_app_id
META_APP_SECRET=your_app_secret
META_WABA_ID=your_waba_id
META_PHONE_NUMBER_ID=your_phone_number_id
META_ACCESS_TOKEN=your_access_token
META_API_VERSION=v22.0

# Webhook
WEBHOOK_BASE_URL=https://your-ngrok-url.ngrok-free.app
WEBHOOK_VERIFY_TOKEN=YourVerifyToken

# Frontend
FRONTEND_URL=http://localhost:5173
CORS_ORIGINS=http://localhost:5173
```

### Frontend `.env`

```env
VITE_API_URL=http://localhost:8002/api/v1
VITE_WS_URL=ws://localhost:8002
```

---

## 📡 Meta WhatsApp Setup

### 1. Create Meta App
1. Go to [developers.facebook.com](https://developers.facebook.com)
2. Create a new app → **Business** type
3. Add **WhatsApp** product
4. Copy your **App ID**, **Phone Number ID**, **WABA ID**, **Access Token**

### 2. Configure Webhook
```
URL:          https://your-domain.com/api/v1/webhook/{tenant_id}
Verify Token: (matches WEBHOOK_VERIFY_TOKEN in .env)
Subscribe:    messages, message_template_status_update
```

Use [ngrok](https://ngrok.com) for local development:
```bash
ngrok http 8002
# Copy the https URL to WEBHOOK_BASE_URL in .env
```

### 3. API Permissions Required
- `whatsapp_business_messaging` ✅ (requires App Review)
- `whatsapp_business_management` ✅

---

## 🔑 Key API Endpoints

### Auth
```
POST /api/v1/auth/register      Register new tenant
POST /api/v1/auth/login         Login → get JWT
POST /api/v1/auth/refresh       Refresh access token
```

### Conversations
```
GET  /api/v1/conversations              List conversations
GET  /api/v1/conversations/{id}/messages  Get messages
POST /api/v1/conversations/{id}/messages  Send message
POST /api/v1/conversations/start        Start new conversation
```

### Templates
```
GET  /api/v1/templates/local    List local templates
POST /api/v1/templates          Create template on Meta
POST /api/v1/templates/sync     Sync from Meta
POST /api/v1/templates/upload-header  Upload image header
POST /api/v1/templates/send/{wa_id}   Send template
```

### Broadcasts
```
POST /api/v1/broadcasts         Create broadcast
GET  /api/v1/broadcasts         List broadcasts
POST /api/v1/broadcasts/{id}/send  Send broadcast
```

### Auto-Reply
```
GET  /api/v1/autoreplies        List rules
POST /api/v1/autoreplies        Create rule
PATCH /api/v1/autoreplies/{id}/toggle  Enable/disable
```

---

## 🤖 Auto-Reply Rules

Create rules in **Automations** page:

| Trigger | Description |
|---|---|
| `any` | Fires on every inbound message |
| `first_message` | Only when conversation starts |
| `keyword` | Contains / exact / starts-with match |
| `outside_hours` | Outside 9am–6pm UTC Mon–Fri |

**Actions:** Text reply or WhatsApp Template  
**Conditions:** First message only, cooldown (minutes)

---

## 📢 Broadcast Campaigns

1. Create broadcast with template + audience (all / tag / hand-pick)
2. Fill variable values for `{{1}}`, `{{2}}` etc.
3. Send immediately or schedule
4. Track delivery in Analytics tab

**Template variable rule:**
- Numeric: `{{1}}`, `{{2}}` → sorted by number
- Named: `{{first_name}}`, `{{order_number}}` → sent in template body order

---

## 🔄 Real-time Updates

WebSocket endpoint: `ws://localhost:8002/api/v1/ws/inbox?token=<JWT>`

Events:
```json
{ "type": "connected" }
{ "type": "new_message", "conversation_id": "...", "message": {...} }
{ "type": "status_update", "wa_message_id": "...", "status": "delivered" }
{ "type": "ping" }
```

---

## 🛠️ Development Scripts

```bash
# Backend — seed test auto-reply rule
python seed_autoreply.py

# Backend — test template creation
python test_template_create.py

# Backend — test broadcast send
python test_broadcast_send.py
```

---

## 📦 Requirements

### Backend (`requirements.txt`)
```
fastapi
uvicorn[standard]
beanie
motor
httpx
python-jose[cryptography]
passlib[bcrypt]
python-dotenv
pydantic-settings
structlog
python-multipart
```

### Frontend
```
react, react-dom, react-router-dom
vite
tailwindcss
axios
```

---

## 🚢 Production Deployment

```bash
# Backend
uvicorn app.main:app --host 0.0.0.0 --port 8002 --workers 4

# Frontend
npm run build
# Serve dist/ with nginx or any static host
```

---

## 📄 License

MIT License — free to use and modify.

---

## 🙏 Built With

- [Meta WhatsApp Cloud API](https://developers.facebook.com/docs/whatsapp/cloud-api)
- [FastAPI](https://fastapi.tiangolo.com)
- [Beanie](https://beanie-odm.dev)
- [React](https://react.dev)
- [Tailwind CSS](https://tailwindcss.com)
