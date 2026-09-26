#!/bin/sh
# Installs the ownership pre-commit hook. Run from the repo root.
set -e
HOOK=.git/hooks/pre-commit
cat > "$HOOK" <<'EOF'
#!/bin/sh
python3 scripts/check_ownership.py || exit 1
EOF
chmod +x "$HOOK"
echo "Installed $HOOK. Your role: $(git config reigns.role || echo 'NOT SET — run: git config reigns.role <A|B|C|D>')"
