package appcommands

import (
	"context"
	"fmt"
	"strings"

	voicecmds "github.com/robinlant/sklep-ds-bot/internal/commands"
	"github.com/robinlant/sklep-ds-bot/internal/shuffle"

	"github.com/bwmarrin/discordgo"
)

func RegisterCommands(_ context.Context, session *discordgo.Session, appID, guildID string) error {
	if session == nil || strings.TrimSpace(appID) == "" || strings.TrimSpace(guildID) == "" {
		return fmt.Errorf("application id and guild id are required")
	}
	commands := Commands()
	_, err := session.ApplicationCommandBulkOverwrite(appID, guildID, commands)
	return err
}

func Commands() []*discordgo.ApplicationCommand {
	return []*discordgo.ApplicationCommand{
		voicecmds.VoiceApplicationCommand(),
		shuffle.ShuffleApplicationCommand(),
	}
}
