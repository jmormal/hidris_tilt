#!/bin/bash

# 1. Name your session and set path to current directory
SESSION="hidris"
PROJECT_ROOT=$(pwd)
K8S_CONTEXT="k3d-hidris"
echo $PROJECT_ROOT
# 2. Check if the session exists
tmux has-session -t $SESSION 2>/dev/null

if [ $? != 0 ]; then
  # --- SESSION CREATION ---

  # Create detached session, Window 1 = 'Editor'
  tmux new-session -d -s $SESSION -n 'Editor'

  # Open Neovim in the first window
  tmux send-keys -t $SESSION:Editor "nvim ." C-m

  # --- FRONTEND ---
  # Create Window 2 = 'Servers'
  #
  tmux new-window -t $SESSION -n 'tilt'
  tmux send-keys -t $SESSION:tilt "tilt up" C-m

  tmux new-window -t $SESSION -n 'k9s'

  # Pane 1 (Left): Frontend
  # Assuming frontend is in a subdirectory named 'frontend'.
  # Remove '/frontend' if it's in the root.
  tmux send-keys -t $SESSION:k9s "kubectl config use-context $K8S_CONTEXT" C-m
  tmux send-keys -t $SESSION:k9s "k9s" C-m # Adjust command if needed
  # Focus on the Editor window
  tmux select-window -t $SESSION:Editor
fi

# 3. Attach to session
tmux attach -t $SESSION
