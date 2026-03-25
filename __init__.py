import json
import uuid
import asyncio
import traceback
from aiohttp import web
from server import PromptServer

# 全局任务状态追踪
pending_signals = {} # { prompt_id: str }
error_logs = {}      # { prompt_id: str }

# --- 1. 核心 Hook：拦截 ComfyUI 事件总线 ---
_orig_send_sync = PromptServer.instance.send_sync
def patched_send_sync(event, data, sid=None):
    pid = data.get("prompt_id") if isinstance(data, dict) else None
    if pid and pid in pending_signals:
        if event == "execution_interrupted":
            pending_signals[pid] = "interrupted"
        elif event == "execution_error":
            error_logs[pid] = data.get("exception_message", "Unknown Node Error")
            pending_signals[pid] = "error"
    return _orig_send_sync(event, data, sid=sid)
PromptServer.instance.send_sync = patched_send_sync

# --- 2. 媒体解析逻辑 (支持图片、音频、文本) ---
def format_outputs(node_output, base_url):
    results = []
    
    # A. 处理图片 (Images)
    if "images" in node_output:
        for img in node_output["images"]:
            url = f"{base_url}/view?filename={img['filename']}&subfolder={img['subfolder']}&type={img['type']}"
            results.append({"type": "image", "value": url})
            
    # B. 处理音频 (Audio) - 解决你提到的 type/链接问题
    if "audio" in node_output:
        for aud in node_output["audio"]:
            url = f"{base_url}/view?filename={aud['filename']}&subfolder={aud['subfolder']}&type={aud['type']}"
            results.append({"type": "audio", "value": url})
            
    # C. 处理文本 (Text) - 解决 JSON/文本解析
    if "text" in node_output:
        for txt in node_output["text"]:
            results.append({"type": "text", "value": txt})
            
    return results

# --- 3. 接口 A：任务提交 (POST) ---
@PromptServer.instance.routes.post("/api/enhanced_run")
async def enhanced_run_api(request):
    try:
        data = await request.json()
        raw_workflow = data.get("workflow", {})
        inputs = data.get("inputs", {})
        target_outputs = [str(x).strip().replace('\xa0', '') for x in data.get("outputs", [])]
        server = PromptServer.instance

        # 预校验：防止 NodeNotFoundError
        workflow_keys = [str(k) for k in raw_workflow.keys()]
        valid_outputs = [tid for tid in target_outputs if tid in workflow_keys]
        if target_outputs and not valid_outputs:
            return web.json_response({"error": f"Invalid Node IDs: {target_outputs}", "status": "failed"}, status=400)

        # 参数注入
        for key, value in inputs.items():
            if "_" in key:
                nid, field = key.split("_", 1)
                if nid.strip() in raw_workflow:
                    raw_workflow[nid.strip()]["inputs"][field] = value

        prompt_id = str(uuid.uuid4())
        pending_signals[prompt_id] = "running"
        
        # --- 核心适配 v0.11.0 的 8 元组与伪装工作流 ---
        # 修复 ShowText 等插件报错 KeyError: 'nodes'
        fake_workflow_structure = {
            "nodes": [{"id": nid, "type": info.get("class_type")} for nid, info in raw_workflow.items()],
            "links": []
        }
        
        extra_data = {
            "client_id": "api_enhanced",
            "extra_pnginfo": { "workflow": fake_workflow_structure }
        }
        
        # 提交 v0.11.0 要求的标准 8 元组 (解决 ValueError 解包问题)
        task_item = (
            server.number,      # [0] priority
            prompt_id,          # [1] prompt_id
            raw_workflow,       # [2] prompt
            extra_data,         # [3] extra_data (包含 workflow 解决 nodes 错误)
            valid_outputs,      # [4] outputs
            {},                 # [5] info
            False,              # [6] is_sensitive
            None                # [7] context
        )
        
        server.prompt_queue.put(task_item)
        server.number += 1

        return web.json_response({"status": "queued", "prompt_id": prompt_id})
    except Exception as e:
        traceback.print_exc()
        return web.json_response({"error": str(e), "status": "failed", "prompt_id": None}, status=500)

# --- 4. 接口 B：状态查询 (GET) ---
@PromptServer.instance.routes.get("/api/get_results")
async def get_results_api(request):
    pid = request.query.get("id")
    if not pid: return web.json_response({"error": "Missing ID"}, status=400)
    
    server = PromptServer.instance
    base_url = f"{request.scheme}://{request.host}"
    
    # A. 检查历史记录 (成功出口)
    history = server.prompt_queue.get_history(prompt_id=pid)
    if pid in history:
        job = history[pid]
        outputs = job.get("outputs", {})
        # 调用增强后的 format_outputs 解析音频和文本
        res = {tid: format_outputs(out, base_url) for tid, out in outputs.items()}
        return web.json_response({"status": "success", "results": res})

    # B. 检查拦截到的中断/错误信号
    sig = pending_signals.get(pid)
    if sig == "interrupted":
        return web.json_response({"status": "failed", "error": "Execution interrupted"})
    if sig == "error":
        return web.json_response({"status": "failed", "error": error_logs.get(pid, "Node error")})

    # C. 检查是否还在队列
    is_active = any(x[1] == pid for x in server.prompt_queue.queue) or pid in server.prompt_queue.currently_running.values()
    if is_active or sig == "running":
        return web.json_response({"status": "processing"})

    return web.json_response({"status": "not_found", "error": "Task disappeared"}, status=404)

NODE_CLASS_MAPPINGS = {}; __all__ = ["NODE_CLASS_MAPPINGS"]