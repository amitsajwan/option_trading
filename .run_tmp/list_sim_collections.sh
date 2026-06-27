#!/bin/bash
sudo docker exec option_trading-mongo-1 mongosh --quiet trading_ai --eval 'db.getCollectionNames().filter(c=>c.includes("sim")).sort().forEach(c=>print(c))'
