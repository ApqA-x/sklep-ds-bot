package main

import (
	"context"
	"log"
	"math/rand"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/robinlant/sklep-ds-bot/internal/appcommands"
	"github.com/robinlant/sklep-ds-bot/internal/config"
	"github.com/robinlant/sklep-ds-bot/internal/shuffle"

	"github.com/bwmarrin/discordgo"
)

func main() {
	cfg, err := config.Load()
	if err != nil {
		log.Fatal(err)
	}
	if cfg.DiscordToken == "" {
		log.Fatal("DISCORD_TOKEN is required")
	}
	if cfg.DiscordApplicationID == "" {
		log.Fatal("DISCORD_APPLICATION_ID is required")
	}
	if cfg.DiscordGuildID == "" {
		log.Fatal("DISCORD_GUILD_ID is required")
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	dg, err := discordgo.New("Bot " + cfg.DiscordToken)
	if err != nil {
		log.Fatal(err)
	}
	dg.StateEnabled = true
	dg.Identify.Intents = discordgo.IntentsGuilds | discordgo.IntentsGuildVoiceStates | discordgo.IntentsGuildMembers

	ready := make(chan string, 1)
	dg.AddHandlerOnce(func(_ *discordgo.Session, event *discordgo.Ready) {
		if event != nil && event.User != nil {
			ready <- event.User.ID
		}
	})

	if err := dg.Open(); err != nil {
		log.Fatal(err)
	}
	defer dg.Close()

	botUserID := <-ready

	service := shuffle.New(dg.State, dg, botUserID, rand.New(rand.NewSource(time.Now().UTC().UnixNano())))
	service.Install(dg, cfg.DiscordGuildID)

	if err := appcommands.RegisterCommands(ctx, dg, cfg.DiscordApplicationID, cfg.DiscordGuildID); err != nil {
		log.Fatal(err)
	}

	<-ctx.Done()
}
