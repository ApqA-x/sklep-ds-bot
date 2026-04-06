package appcommands

import (
	"context"
	"fmt"
	"strings"

	voicecmds "github.com/robinlant/sklep-ds-bot/internal/commands"
	"github.com/robinlant/sklep-ds-bot/internal/shuffle"

	"github.com/bwmarrin/discordgo"
)

var commandCatalog = defaultCommands

func RegisterCommands(_ context.Context, session *discordgo.Session, appID, guildID string) error {
	if session == nil || strings.TrimSpace(appID) == "" || strings.TrimSpace(guildID) == "" {
		return fmt.Errorf("application id and guild id are required")
	}
	commands := commandCatalog()
	if err := validateCommands(commands); err != nil {
		return err
	}
	_, err := session.ApplicationCommandBulkOverwrite(appID, guildID, commands)
	return err
}

func Commands() []*discordgo.ApplicationCommand {
	return commandCatalog()
}

func defaultCommands() []*discordgo.ApplicationCommand {
	return []*discordgo.ApplicationCommand{
		voicecmds.VoiceApplicationCommand(),
		shuffle.ShuffleApplicationCommand(),
	}
}

func validateCommands(commands []*discordgo.ApplicationCommand) error {
	if len(commands) == 0 {
		return fmt.Errorf("no application commands to register")
	}
	seen := make(map[string]struct{}, len(commands))
	for i, command := range commands {
		if command == nil || strings.TrimSpace(command.Name) == "" {
			return fmt.Errorf("invalid application command at index %d", i)
		}
		name := strings.TrimSpace(command.Name)
		if _, ok := seen[name]; ok {
			return fmt.Errorf("duplicate application command %q", name)
		}
		seen[name] = struct{}{}
	}
	return nil
}
