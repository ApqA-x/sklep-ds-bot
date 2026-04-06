package main

import (
	"context"
	"encoding/json"
	"log"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/robinlant/sklep-ds-bot/internal/bus"
	"github.com/robinlant/sklep-ds-bot/internal/config"
	"github.com/robinlant/sklep-ds-bot/internal/domain"
	"github.com/robinlant/sklep-ds-bot/internal/gateway"
	mongorepo "github.com/robinlant/sklep-ds-bot/internal/mongo"

	"github.com/bwmarrin/discordgo"
	"go.mongodb.org/mongo-driver/mongo"
	"go.mongodb.org/mongo-driver/mongo/options"
)

const summaryEmbedColor = 0x5865F2

func main() {
	cfg, err := config.Load()
	if err != nil {
		log.Fatal(err)
	}
	if cfg.DiscordToken == "" {
		log.Fatal("DISCORD_TOKEN is required")
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	if cfg.EventSigningSecret == "" {
		log.Fatal("EVENT_SIGNING_SECRET is required")
	}
	mongoClient, err := mongo.Connect(ctx, options.Client().ApplyURI(cfg.MongoURI))
	if err != nil {
		log.Fatal(err)
	}
	defer func() { _ = mongoClient.Disconnect(context.Background()) }()
	repo := mongorepo.NewRepository(mongoClient.Database(cfg.MongoDB))

	busConn, err := bus.Connect(cfg.NATSURL, cfg.EventSigningSecret, "gateway")
	if err != nil {
		log.Fatal(err)
	}
	defer busConn.Close()

	dg, err := discordgo.New("Bot " + cfg.DiscordToken)
	if err != nil {
		log.Fatal(err)
	}
	dg.StateEnabled = true
	dg.Identify.Intents = discordgo.IntentsGuilds | discordgo.IntentsGuildVoiceStates | discordgo.IntentsGuildMembers

	service := gateway.New(dg, busConn)
	service.Install()

	if err := dg.Open(); err != nil {
		log.Fatal(err)
	}
	defer dg.Close()

	deliverPending := func() error {
		sessions, err := repo.ListSummariesPendingDelivery(ctx)
		if err != nil {
			return err
		}
		for _, session := range sessions {
			if session.SummaryChannelID == "" || session.SummaryMessage == "" {
				continue
			}
			claimed, err := repo.ClaimSessionSummaryDelivery(ctx, session.ID, time.Now().UTC())
			if err != nil || !claimed {
				continue
			}
			if _, err := dg.ChannelMessageSend(session.SummaryChannelID, session.SummaryMessage); err != nil {
				log.Printf("gateway delivery skipped session=%s: %v", session.ID, err)
				if relErr := repo.ReleaseSessionSummaryDeliveryClaim(ctx, session.ID); relErr != nil {
					log.Printf("gateway release claim error session=%s: %v", session.ID, relErr)
				}
				continue
			}
			if err := repo.MarkSessionSummaryDelivered(ctx, session.ID, time.Now().UTC()); err != nil {
				log.Printf("gateway mark delivered skipped session=%s: %v", session.ID, err)
				continue
			}
		}
		return nil
	}
	if err := deliverPending(); err != nil {
		log.Printf("gateway initial sweep error: %v", err)
	}
	go func() {
		ticker := time.NewTicker(time.Minute)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				if err := deliverPending(); err != nil {
					log.Printf("gateway sweep error: %v", err)
				}
			}
		}
	}()

	sendSummary := func(sessionID, guildID, channelID, message string) error {
		_, err := dg.ChannelMessageSendComplex(channelID, &discordgo.MessageSend{
			Embeds: []*discordgo.MessageEmbed{{
				Title:       "Voice Session Summary",
				Description: message,
				Color:       summaryEmbedColor,
				Footer:      &discordgo.MessageEmbedFooter{Text: "Voice Tracker"},
			}},
		})
		if err != nil {
			log.Printf("gateway summary send failed session=%s guild=%s channel=%s err=%v", sessionID, guildID, channelID, err)
			return err
		}
		log.Printf("gateway summary sent session=%s guild=%s channel=%s", sessionID, guildID, channelID)
		return nil
	}

	_, err = busConn.Subscribe(ctx, domain.SubjectSummaryReady, repo, func(payload []byte) error {
		var event domain.SummaryReadyEvent
		if err := json.Unmarshal(payload, &event); err != nil {
			return err
		}
		if event.ChannelID == "" || event.Message == "" {
			return nil
		}
		claimed, err := repo.ClaimSessionSummaryDelivery(ctx, event.SessionID, time.Now().UTC())
		if err != nil || !claimed {
			if err != nil {
				log.Printf("gateway summary claim failed session=%s guild=%s channel=%s err=%v", event.SessionID, event.GuildID, event.ChannelID, err)
			}
			return nil
		}
		var sendErr error
		for attempt := 0; attempt < 3; attempt++ {
			log.Printf("gateway summary attempt session=%s guild=%s channel=%s attempt=%d", event.SessionID, event.GuildID, event.ChannelID, attempt+1)
			sendErr = sendSummary(event.SessionID, event.GuildID, event.ChannelID, event.Message)
			if sendErr == nil {
				if err := repo.MarkSessionSummaryDelivered(ctx, event.SessionID, time.Now().UTC()); err != nil {
					log.Printf("gateway mark delivered error session=%s: %v", event.SessionID, err)
					return nil
				}
				return nil
			}
			log.Printf("gateway summary retry session=%s guild=%s channel=%s attempt=%d err=%v", event.SessionID, event.GuildID, event.ChannelID, attempt+1, sendErr)
			time.Sleep(time.Duration(attempt+1) * 250 * time.Millisecond)
		}
		if relErr := repo.ReleaseSessionSummaryDeliveryClaim(ctx, event.SessionID); relErr != nil {
			log.Printf("gateway release claim error session=%s: %v", event.SessionID, relErr)
		}
		return sendErr
	})
	if err != nil {
		log.Fatal(err)
	}

	<-ctx.Done()
}
