#!/system/bin/sh
# /data/adb/modules/hyperos_sms_forwarder/core/sms_daemon.sh
# 备用机全方位通信底层转发 (V1 稳定双气泡 + 万能关键词 + 完美日志版)

BASE_DIR=$(dirname $(readlink -f "$0"))
CONFIG_FILE="$BASE_DIR/config.conf"

# 💡 补回缺失的目录和日志定义
TMP_DIR=${TMP_DIR:-"/data/local/tmp"}
LOG_FILE="$TMP_DIR/sms_forward_run.log"
MAX_LOG_SIZE_KB=${MAX_LOG_SIZE_KB:-1024}

# ================= 核心工具函数 =================

log_msg() {
    local level=$1
    local msg=$2
    local time=$(date "+%Y-%m-%d %H:%M:%S")
    echo "[$time] [$level] $msg" >> "$LOG_FILE"
    
    # 日志防爆满清理逻辑
    if [ -f "$LOG_FILE" ]; then
        local size_bytes=$(wc -c < "$LOG_FILE" | tr -d ' ')
        local max_bytes=$((MAX_LOG_SIZE_KB * 1024))
        if [ "$size_bytes" -ge "$max_bytes" ]; then
            mv "$LOG_FILE" "${LOG_FILE}.bak"
            echo "[$time] [INFO] 日志体积达标，触发自动清理。" > "$LOG_FILE"
        fi
    fi
}

escape_json() {
    echo "$1" | awk '{gsub(/\\/, "\\\\"); gsub(/"/, "\\\""); printf "%s\\n", $0}' | sed 's/\\n$//'
}

send_to_webhook() {
    # 强制在标题加入“通知”二字，突破钉钉拦截
    local safe_title=$(escape_json "【通知】$1") 
    local safe_text=$(escape_json "$2")
    
    local PAYLOAD="$TMP_DIR/ding_payload_$RANDOM.json"
    printf '{"msgtype": "markdown", "markdown": {"title": "%s", "text": "%s"}}' "$safe_title" "$safe_text" > "$PAYLOAD"
    
    local response=$(curl -s -w "\n%{http_code}" -H "Content-Type: application/json" -d "@$PAYLOAD" "$WEBHOOK_URL")
    local http_code=$(echo "$response" | tail -n 1)
    
    rm -f "$PAYLOAD"

    if [ "$http_code" = "200" ]; then
        log_msg "SUCCESS" "推送钉钉成功！"
        return 0
    else
        log_msg "ERROR" "推送钉钉失败！HTTP状态码: $http_code"
        return 1
    fi
}

init_timestamp() {
    local uri=$1
    local where_cond=$2
    local record_file=$3
    if [ ! -f "$record_file" ]; then
        local raw_data=""
        if [ -n "$where_cond" ]; then
            raw_data=$(content query --uri "$uri" --projection date --where "$where_cond" --sort "date DESC" 2>/dev/null | head -n 1)
        else
            raw_data=$(content query --uri "$uri" --projection date --sort "date DESC" 2>/dev/null | head -n 1)
        fi
        local init_date=$(echo "$raw_data" | sed -n 's/.*date=\([^,]*\).*/\1/p' | tr -cd '0-9')
        [ -z "$init_date" ] && init_date=0
        echo "$init_date" > "$record_file"
        log_msg "INFO" "初始化时间戳: $record_file -> $init_date"
    fi
}

# ================= 业务逻辑函数 =================

check_sms() {
    local record_file="$TMP_DIR/last_sms_date.txt"
    local sms_uri="content://sms/inbox"
    local where_cond=""
    
    if [ "$OS_TYPE" = "ColorOS" ]; then
        sms_uri="content://sms"
        where_cond="type=1"
    fi

    init_timestamp "$sms_uri" "$where_cond" "$record_file"
    local last_date=$(cat "$record_file" | tr -cd '0-9')
    [ -z "$last_date" ] && last_date=0
    
    local raw_sms=""
    if [ -n "$where_cond" ]; then
        raw_sms=$(content query --uri "$sms_uri" --projection address:body:date:sub_id --where "$where_cond" --sort "date DESC" 2>/dev/null | head -n 1)
    else
        raw_sms=$(content query --uri "$sms_uri" --projection address:body:date:sub_id --sort "date DESC" 2>/dev/null | head -n 1)
    fi

    if [ -n "$raw_sms" ]; then
        local address=$(echo "$raw_sms" | sed 's/.*address=\(.*\), body=.*/\1/')
        local body=$(echo "$raw_sms" | sed 's/.*body=\(.*\), date=.*/\1/')
        local sms_date=$(echo "$raw_sms" | sed -n 's/.*date=\([^,]*\).*/\1/p' | tr -cd '0-9')
        local sub_id=$(echo "$raw_sms" | sed -n 's/.*sub_id=\([^,]*\).*/\1/p')

        if [ -n "$sms_date" ] && [ "$sms_date" -gt "$last_date" ]; then
            local sim_tag="未知"
            [ "$sub_id" = "1" ] || [ "$sub_id" = "0" ] && sim_tag="卡1"
            [ "$sub_id" = "2" ] && sim_tag="卡2"

            log_msg "INFO" "捕获新短信 -> 号码: $address"
            
            # 防贪婪精准提取补丁
            local safe_body=" $body"
            local vcode=$(echo "$safe_body" | sed -n 's/.*[^0-9]\([0-9]\{4,8\}\)[^0-9]*验证码.*/\1/p')
            [ -z "$vcode" ] && vcode=$(echo "$safe_body" | sed -n 's/.*验证码[^0-9]*\([0-9]\{4,8\}\).*/\1/p')
            [ -z "$vcode" ] && vcode=$(echo "$safe_body" | sed -n 's/.*[Cc]ode[^0-9]*\([0-9]\{4,8\}\).*/\1/p')

            local cb='```'
            if [ -n "$vcode" ]; then
                local detail_msg="### 💬 ${KEYWORD_SMS} 详情\n\n---\n- **卡槽**: ${sim_tag}\n- **发件人**: ${address}\n- **内容**: ${body}"
                send_to_webhook "${KEYWORD_SMS}" "$detail_msg"
                
                local vcode_msg="${cb}text\n${vcode}\n${cb}"
                if send_to_webhook "${KEYWORD_SMS}" "$vcode_msg"; then
                    echo "$sms_date" > "$record_file"
                fi
            else
                local normal_msg="### 💬 ${KEYWORD_SMS}\n\n---\n- **卡槽**: ${sim_tag}\n- **发件人**: ${address}\n- **内容**: ${body}"
                if send_to_webhook "${KEYWORD_SMS}" "$normal_msg"; then
                    echo "$sms_date" > "$record_file"
                fi
            fi
        fi
    fi
}

check_call_log() {
    local call_type=$1
    local keyword=$2
    local record_file="$TMP_DIR/last_call_${call_type}_date.txt"
    
    if [ ! -f "$record_file" ]; then
        local raw_init=$(content query --uri content://call_log/calls --projection date:type --sort "date DESC" 2>/dev/null | head -n 15 | grep "type=${call_type}" | head -n 1)
        local init_date=$(echo "$raw_init" | sed -n 's/.*date=\([^,]*\).*/\1/p' | tr -cd '0-9')
        [ -z "$init_date" ] && init_date=0
        echo "$init_date" > "$record_file"
        log_msg "INFO" "通话初始化: $record_file -> $init_date"
    fi
    
    local last_date=$(cat "$record_file" | tr -cd '0-9')
    [ -z "$last_date" ] && last_date=0
    
    local raw_call=$(content query --uri content://call_log/calls --projection number:date:type --sort "date DESC" 2>/dev/null | head -n 15 | grep "type=${call_type}" | head -n 1)

    if [ -n "$raw_call" ]; then
        local number=$(echo "$raw_call" | sed -n 's/.*number=\([^,]*\).*/\1/p')
        local call_date=$(echo "$raw_call" | sed -n 's/.*date=\([^,]*\).*/\1/p' | tr -cd '0-9')

        if [ -n "$call_date" ] && [ "$call_date" -gt "$last_date" ]; then
            log_msg "INFO" "捕获通话记录(类型$call_type) -> 号码: $number"
            
            local message="### 📞 ${keyword}\n\n---\n- **号码**: ${number}"
            
            if send_to_webhook "${keyword}" "$message"; then
                echo "$call_date" > "$record_file"
            fi
        fi
    fi
}

# ================= 启动准备 =================

if [ -f "$CONFIG_FILE" ]; then
    source "$CONFIG_FILE"
    touch "$LOG_FILE"
    log_msg "INFO" "========================================="
    log_msg "INFO" "通信转发守护进程启动 [完美日志修复版]"
else
    echo "致命错误：找不到配置文件 $CONFIG_FILE"
    exit 1
fi

# ================= 守护进程主循环 =================

while true; do
    if [ "$ENABLE_SMS" = "true" ]; then check_sms; fi
    if [ "$ENABLE_MISSED_CALL" = "true" ]; then check_call_log "3" "$KEYWORD_MISSED"; fi
    if [ "$ENABLE_INCOMING_CALL" = "true" ]; then check_call_log "1" "$KEYWORD_INCOMING"; fi
    if [ "$ENABLE_OUTGOING_CALL" = "true" ]; then check_call_log "2" "$KEYWORD_OUTGOING"; fi
    sleep "$INTERVAL"
done