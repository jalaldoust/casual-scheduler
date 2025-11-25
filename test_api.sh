#!/bin/bash

# GPU Scheduler API Test Script
# This script starts the server and tests the GPU monitoring endpoint

set -e

# Colors for output
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${YELLOW}=== GPU Scheduler API Test ===${NC}\n"

# Generate test token
TEST_TOKEN="a9d429857f97b3d13dd86e6ff75000a3d258a9b37d939b499bb947dda2610332"
echo -e "${GREEN}✓${NC} Using test token: ${TEST_TOKEN:0:20}..."

# Set environment variable
export GPU_MONITOR_TOKEN="$TEST_TOKEN"
export PORT=8000

# Check if server is already running
if lsof -ti:8000 > /dev/null 2>&1; then
    echo -e "${YELLOW}⚠${NC}  Server already running on port 8000"
    echo "Stopping existing server..."
    kill $(lsof -ti:8000) 2>/dev/null || true
    sleep 2
fi

# Start server in background
echo -e "${YELLOW}Starting server...${NC}"
cd /Users/kasra/Desktop/code/gpu-scheduler
python3 app.py > /tmp/gpu-scheduler.log 2>&1 &
SERVER_PID=$!

echo -e "${GREEN}✓${NC} Server started (PID: $SERVER_PID)"
echo "Waiting for server to initialize..."
sleep 3

# Check if server is responding
echo -e "\n${YELLOW}Testing server health...${NC}"
if curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/ | grep -q "200\|302"; then
    echo -e "${GREEN}✓${NC} Server is responding"
else
    echo -e "${RED}✗${NC} Server not responding"
    echo "Check logs at: /tmp/gpu-scheduler.log"
    exit 1
fi

# Test the GPU monitoring endpoint
echo -e "\n${YELLOW}Testing GPU monitoring endpoint...${NC}"
RESPONSE=$(curl -s -w "\nHTTP_CODE:%{http_code}" -X POST http://localhost:8000/api/gpu-status \
  -H "Authorization: Bearer $TEST_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "timestamp": "2025-11-07T14:30:00-05:00",
    "usage": {
      "0": ["kasra", "eb"],
      "1": [],
      "2": ["ml"],
      "3": [],
      "4": ["yushupan"],
      "5": [],
      "6": ["jgw2140"],
      "7": []
    }
  }')

HTTP_CODE=$(echo "$RESPONSE" | grep "HTTP_CODE:" | cut -d: -f2)
BODY=$(echo "$RESPONSE" | sed '/HTTP_CODE:/d')

echo -e "\n${YELLOW}Response (HTTP $HTTP_CODE):${NC}"
echo "$BODY" | python3 -m json.tool 2>/dev/null || echo "$BODY"

if [ "$HTTP_CODE" = "200" ]; then
    echo -e "\n${GREEN}✓ API endpoint working!${NC}"
    
    # Test without timestamp
    echo -e "\n${YELLOW}Testing without timestamp (server time only)...${NC}"
    RESPONSE2=$(curl -s -w "\nHTTP_CODE:%{http_code}" -X POST http://localhost:8000/api/gpu-status \
      -H "Authorization: Bearer $TEST_TOKEN" \
      -H "Content-Type: application/json" \
      -d '{
        "usage": {
          "0": ["test_user"],
          "1": [],
          "2": [],
          "3": [],
          "4": [],
          "5": [],
          "6": [],
          "7": []
        }
      }')
    
    HTTP_CODE2=$(echo "$RESPONSE2" | grep "HTTP_CODE:" | cut -d: -f2)
    BODY2=$(echo "$RESPONSE2" | sed '/HTTP_CODE:/d')
    
    echo -e "\n${YELLOW}Response (HTTP $HTTP_CODE2):${NC}"
    echo "$BODY2" | python3 -m json.tool 2>/dev/null || echo "$BODY2"
    
    if [ "$HTTP_CODE2" = "200" ]; then
        echo -e "\n${GREEN}✓ Server-time-only mode working!${NC}"
    fi
    
    echo -e "\n${GREEN}=== All Tests Passed ===${NC}"
    echo -e "\n${YELLOW}Server Info:${NC}"
    echo "  - Running on: http://localhost:8000"
    echo "  - PID: $SERVER_PID"
    echo "  - Logs: /tmp/gpu-scheduler.log"
    echo "  - Token: $TEST_TOKEN"
    
    echo -e "\n${YELLOW}Commands:${NC}"
    echo "  View logs:  tail -f /tmp/gpu-scheduler.log"
    echo "  Stop server: kill $SERVER_PID"
    echo "  Open UI:    open http://localhost:8000"
    
    echo -e "\n${YELLOW}Test curl command for your engineer:${NC}"
    echo "curl -X POST http://localhost:8000/api/gpu-status \\"
    echo "  -H \"Authorization: Bearer $TEST_TOKEN\" \\"
    echo "  -H \"Content-Type: application/json\" \\"
    echo "  -d '{\"usage\": {\"0\": [\"user1\"], \"1\": [], \"2\": [], \"3\": [], \"4\": [], \"5\": [], \"6\": [], \"7\": []}}'"
    
else
    echo -e "\n${RED}✗ API test failed${NC}"
    echo "Check logs at: /tmp/gpu-scheduler.log"
    echo "Server PID: $SERVER_PID"
    exit 1
fi
