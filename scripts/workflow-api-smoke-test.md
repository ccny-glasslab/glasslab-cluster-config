# Workflow-API Quick Smoke Test

Run in order. Stop at first failure.

```bash
# 1) Pod must be Ready (NOT 0/1 Running)
POD=$(kubectl -n glasslab-v2 get pods -l app.kubernetes.io/name=glasslab-workflow-api -o name | cut -d'/' -f2)
kubectl -n glasslab-v2 get pod $POD

# 2) Health endpoint from inside pod
kubectl -n glasslab-v2 exec $POD -- python3 -c 'import urllib.request; print(urllib.request.urlopen("http://localhost:8080/healthz", timeout=5).read().decode())'

# 3) Service IP from another pod (cross-node test)
kubectl -n glasslab-v2 run smoke-test --image=nicolaka/netshoot --restart=Never --command -- sleep 300
SVC_IP=$(kubectl -n glasslab-v2 get svc glasslab-workflow-api -o jsonpath='{.spec.clusterIP}')
kubectl -n glasslab-v2 exec smoke-test -- curl -s http://$SVC_IP:8080/healthz

# 4) Port-forward from .44
# Run in one shell on .44: kubectl -n glasslab-v2 port-forward deploy/glasslab-workflow-api 19000:8080
# Then test: curl http://127.0.0.1:19000/healthz

# 5) GPU job submission
SESSION=$(curl -s -X POST http://127.0.0.1:19000/research-sessions -H 'Content-Type: application/json' -d '{"title":"smoke test","goal_statement":"test","submitted_by":"smoke-test"}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["session_id"])')
RUN=$(curl -s -X POST http://127.0.0.1:19000/runs -H 'Content-Type: application/json' -d "{\"workflow_id\":\"gpu-experiment\",\"objective\":\"smoke\",\"inputs\":{},\"models\":[\"pytorch-template-v1\"]}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["run_id"])')
echo "Submitted run: $RUN"

# 6) Cleanup
kubectl -n glasslab-v2 delete pod smoke-test
kubectl -n glasslab-v2 delete jobs -l app=glasslab-titanic 2>/dev/null || true
```

If all steps succeed → workflow-api and Calico are healthy.
