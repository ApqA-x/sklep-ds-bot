package appcommands

import (
	"testing"

	"github.com/bwmarrin/discordgo"
)

func TestRegisterCommandsRejectsEmptyCatalog(t *testing.T) {
	prev := commandCatalog
	commandCatalog = func() []*discordgo.ApplicationCommand { return nil }
	defer func() { commandCatalog = prev }()

	if err := RegisterCommands(nil, &discordgo.Session{}, "app", "guild"); err == nil {
		t.Fatal("expected empty catalog error")
	}
}

func TestRegisterCommandsRejectsMalformedCatalog(t *testing.T) {
	prev := commandCatalog
	commandCatalog = func() []*discordgo.ApplicationCommand { return []*discordgo.ApplicationCommand{{}} }
	defer func() { commandCatalog = prev }()

	if err := RegisterCommands(nil, &discordgo.Session{}, "app", "guild"); err == nil {
		t.Fatal("expected malformed catalog error")
	}
}

func TestRegisterCommandsRejectsNilCommand(t *testing.T) {
	prev := commandCatalog
	commandCatalog = func() []*discordgo.ApplicationCommand { return []*discordgo.ApplicationCommand{nil} }
	defer func() { commandCatalog = prev }()

	if err := RegisterCommands(nil, &discordgo.Session{}, "app", "guild"); err == nil {
		t.Fatal("expected nil command error")
	}
}

func TestRegisterCommandsRejectsDuplicateNames(t *testing.T) {
	prev := commandCatalog
	commandCatalog = func() []*discordgo.ApplicationCommand {
		return []*discordgo.ApplicationCommand{{Name: "dup"}, {Name: "dup"}}
	}
	defer func() { commandCatalog = prev }()

	if err := RegisterCommands(nil, &discordgo.Session{}, "app", "guild"); err == nil {
		t.Fatal("expected duplicate name error")
	}
}
