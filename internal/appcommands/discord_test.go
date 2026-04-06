package appcommands

import (
	"testing"

	voicecmds "github.com/robinlant/sklep-ds-bot/internal/commands"
	"github.com/robinlant/sklep-ds-bot/internal/shuffle"
)

func TestCommandsIncludeVoiceAndShuffle(t *testing.T) {
	commands := Commands()
	if len(commands) != 2 {
		t.Fatalf("expected 2 commands, got %d", len(commands))
	}
	if commands[0].Name != voicecmds.VoiceApplicationCommand().Name {
		t.Fatalf("unexpected first command: %s", commands[0].Name)
	}
	if commands[1].Name != shuffle.ShuffleApplicationCommand().Name {
		t.Fatalf("unexpected second command: %s", commands[1].Name)
	}
}
