#!/bin/sh
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
sh ./start-local-sender.sh "$@"
result=$?
printf '\n按回车关闭此窗口。'
read -r ignored
exit "$result"
