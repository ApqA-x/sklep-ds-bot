package main

import (
	"context"
	"fmt"
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
	log.Printf("shuffle service starting guild=%s", cfg.DiscordGuildID)

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

	botUserID, err := waitForBotUserID(ctx, ready, 30*time.Second)
	if err != nil {
		log.Fatal(err)
	}
	log.Printf("shuffle service ready guild=%s", cfg.DiscordGuildID)

	service := shuffle.New(dg.State, dg, botUserID, rand.New(rand.NewSource(time.Now().UTC().UnixNano())))
	service.Install(dg, cfg.DiscordGuildID, cfg.BotAdminUserIDs)

	if err := appcommands.RegisterCommands(ctx, dg, cfg.DiscordApplicationID, cfg.DiscordGuildID); err != nil {
		log.Fatal(err)
	}
	log.Printf("shuffle commands registered count=%d guild=%s", len(appcommands.Commands()), cfg.DiscordGuildID)

	<-ctx.Done()
}

func waitForBotUserID(ctx context.Context, ready <-chan string, timeout time.Duration) (string, error) {
	timer := time.NewTimer(timeout)
	defer timer.Stop()
	select {
	case botUserID := <-ready:
		if botUserID == "" {
			return "", fmt.Errorf("discord ready event missing bot user id")
		}
		return botUserID, nil
	case <-timer.C:
		return "", fmt.Errorf("timeout waiting for discord ready event: %w", context.DeadlineExceeded)
	case <-ctx.Done():
		return "", ctx.Err()
	}
}
