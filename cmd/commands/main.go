package main

import (
	"context"
	"log"
	"os"
	"os/signal"
	"syscall"

	"github.com/robinlant/sklep-ds-bot/internal/appcommands"
	"github.com/robinlant/sklep-ds-bot/internal/commands"
	"github.com/robinlant/sklep-ds-bot/internal/config"
	mongorepo "github.com/robinlant/sklep-ds-bot/internal/mongo"

	"github.com/bwmarrin/discordgo"
	"go.mongodb.org/mongo-driver/mongo"
	"go.mongodb.org/mongo-driver/mongo/options"
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
	log.Printf("commands service starting guild=%s", cfg.DiscordGuildID)

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	mongoClient, err := mongo.Connect(ctx, options.Client().ApplyURI(cfg.MongoURI))
	if err != nil {
		log.Fatal(err)
	}
	defer func() { _ = mongoClient.Disconnect(context.Background()) }()

	repo := mongorepo.NewRepository(mongoClient.Database(cfg.MongoDB))
	if err := repo.EnsureIndexes(ctx); err != nil {
		log.Fatal(err)
	}

	dg, err := discordgo.New("Bot " + cfg.DiscordToken)
	if err != nil {
		log.Fatal(err)
	}
	dg.StateEnabled = true
	dg.Identify.Intents = discordgo.IntentsGuilds

	service := commands.New(repo)
	service.Install(dg, cfg.DiscordGuildID, cfg.BotAdminUserIDs)

	if err := dg.Open(); err != nil {
		log.Fatal(err)
	}
	defer dg.Close()

	if err := appcommands.RegisterCommands(ctx, dg, cfg.DiscordApplicationID, cfg.DiscordGuildID); err != nil {
		log.Fatal(err)
	}
	log.Printf("application commands registered count=%d guild=%s", len(appcommands.Commands()), cfg.DiscordGuildID)

	<-ctx.Done()
}
