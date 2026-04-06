package mongo

import (
	"context"
	"time"

	"github.com/robinlant/sklep-ds-bot/internal/domain"

	"github.com/google/uuid"
	"go.mongodb.org/mongo-driver/bson"
	"go.mongodb.org/mongo-driver/mongo"
	"go.mongodb.org/mongo-driver/mongo/options"
)

type Repository struct {
	db            *mongo.Database
	guildSettings *mongo.Collection
	messages      *mongo.Collection
	sessions      *mongo.Collection
	participants  *mongo.Collection
}

func NewRepository(db *mongo.Database) *Repository {
	return &Repository{
		db:            db,
		guildSettings: db.Collection("guild_settings"),
		messages:      db.Collection("processed_messages"),
		sessions:      db.Collection("voice_sessions"),
		participants:  db.Collection("voice_session_participants"),
	}
}

func (r *Repository) EnsureIndexes(ctx context.Context) error {
	if _, err := r.sessions.Indexes().CreateMany(ctx, []mongo.IndexModel{
		{
			Keys: bson.D{{Key: "status", Value: 1}, {Key: "guildId", Value: 1}, {Key: "channelId", Value: 1}},
			Options: options.Index().SetUnique(true).SetPartialFilterExpression(bson.M{
				"status": domain.SessionStatusActive,
			}),
		},
		{Keys: bson.D{{Key: "status", Value: 1}, {Key: "guildId", Value: 1}, {Key: "channelId", Value: 1}, {Key: "endedAt", Value: -1}}},
		{Keys: bson.D{{Key: "status", Value: 1}, {Key: "closedEventPublishedAt", Value: 1}}},
	}); err != nil {
		return err
	}
	if _, err := r.participants.Indexes().CreateMany(ctx, []mongo.IndexModel{
		{Keys: bson.D{{Key: "sessionId", Value: 1}, {Key: "active", Value: 1}}},
		{Keys: bson.D{{Key: "guildId", Value: 1}, {Key: "sessionId", Value: 1}, {Key: "active", Value: 1}}},
		{Keys: bson.D{{Key: "guildId", Value: 1}, {Key: "channelId", Value: 1}, {Key: "sessionId", Value: 1}}},
		{
			Keys: bson.D{{Key: "sessionId", Value: 1}, {Key: "userId", Value: 1}, {Key: "active", Value: 1}},
			Options: options.Index().SetUnique(true).SetPartialFilterExpression(bson.M{
				"active": true,
			}),
		},
	}); err != nil {
		return err
	}
	if _, err := r.messages.Indexes().CreateMany(ctx, []mongo.IndexModel{
		{Keys: bson.D{{Key: "subject", Value: 1}, {Key: "messageId", Value: 1}}, Options: options.Index().SetUnique(true)},
	}); err != nil {
		return err
	}
	return nil
}

func (r *Repository) ClaimMessage(ctx context.Context, subject, messageID, issuer string, issuedAt int64) (bool, error) {
	_, err := r.messages.InsertOne(ctx, bson.M{
		"subject":   subject,
		"messageId": messageID,
		"issuer":    issuer,
		"issuedAt":  issuedAt,
		"createdAt": time.Now().UTC(),
	})
	if err != nil {
		if mongo.IsDuplicateKeyError(err) {
			return false, nil
		}
		return false, err
	}
	return true, nil
}

func (r *Repository) GetGuildSettings(ctx context.Context, guildID string) (*domain.GuildSettings, error) {
	var settings domain.GuildSettings
	if err := r.guildSettings.FindOne(ctx, bson.M{"_id": guildID}).Decode(&settings); err != nil {
		if err == mongo.ErrNoDocuments {
			return nil, nil
		}
		return nil, err
	}
	return &settings, nil
}

func (r *Repository) UpsertGuildSettings(ctx context.Context, settings *domain.GuildSettings) error {
	if settings == nil {
		return nil
	}
	if settings.GuildID == "" {
		return nil
	}
	now := time.Now().UTC()
	if settings.CreatedAt.IsZero() {
		settings.CreatedAt = now
	}
	settings.UpdatedAt = now
	_, err := r.guildSettings.UpdateOne(ctx, bson.M{"_id": settings.GuildID}, bson.M{
		"$set": bson.M{
			"trackingMode":      settings.TrackingMode,
			"trackedChannelIds": settings.TrackedChannelIDs,
			"summaryChannelId":  settings.SummaryChannelID,
			"updatedAt":         settings.UpdatedAt,
		},
		"$setOnInsert": bson.M{"createdAt": settings.CreatedAt},
	}, options.Update().SetUpsert(true))
	return err
}

func (r *Repository) ListClosedSessionsPendingNotification(ctx context.Context) ([]domain.Session, error) {
	filter := bson.M{
		"status": domain.SessionStatusClosed,
		"$or": []bson.M{
			{"closedEventPublishedAt": bson.M{"$exists": false}},
			{"closedEventPublishedAt": nil},
		},
	}
	cursor, err := r.sessions.Find(ctx, filter)
	if err != nil {
		return nil, err
	}
	defer cursor.Close(ctx)

	var sessions []domain.Session
	if err := cursor.All(ctx, &sessions); err != nil {
		return nil, err
	}
	return sessions, nil
}

func (r *Repository) ListClosedSessionsPendingSummary(ctx context.Context) ([]domain.Session, error) {
	filter := bson.M{
		"status": domain.SessionStatusClosed,
		"$or": []bson.M{
			{"summaryGeneratedAt": bson.M{"$exists": false}},
			{"summaryGeneratedAt": nil},
		},
	}
	cursor, err := r.sessions.Find(ctx, filter)
	if err != nil {
		return nil, err
	}
	defer cursor.Close(ctx)

	var sessions []domain.Session
	if err := cursor.All(ctx, &sessions); err != nil {
		return nil, err
	}
	return sessions, nil
}

func (r *Repository) ListClosedSessionsByGuildChannel(ctx context.Context, guildID, channelID string, limit int) ([]domain.Session, error) {
	if limit <= 0 {
		limit = 1
	}
	filter := bson.M{
		"status":    domain.SessionStatusClosed,
		"guildId":   guildID,
		"channelId": channelID,
	}
	findOptions := options.Find().SetSort(bson.D{{Key: "endedAt", Value: -1}, {Key: "startedAt", Value: -1}}).SetLimit(int64(limit))
	cursor, err := r.sessions.Find(ctx, filter, findOptions)
	if err != nil {
		return nil, err
	}
	defer cursor.Close(ctx)

	var sessions []domain.Session
	if err := cursor.All(ctx, &sessions); err != nil {
		return nil, err
	}
	return sessions, nil
}

func (r *Repository) ListSummariesPendingDelivery(ctx context.Context) ([]domain.Session, error) {
	filter := bson.M{
		"summaryGeneratedAt": bson.M{"$exists": true},
		"$and": []bson.M{
			{
				"$or": []bson.M{
					{"summaryDeliveredAt": bson.M{"$exists": false}},
					{"summaryDeliveredAt": nil},
				},
			},
			{
				"$or": []bson.M{
					{"summaryDeliveryClaimedAt": bson.M{"$exists": false}},
					{"summaryDeliveryClaimedAt": nil},
				},
			},
		},
	}
	cursor, err := r.sessions.Find(ctx, filter)
	if err != nil {
		return nil, err
	}
	defer cursor.Close(ctx)

	var sessions []domain.Session
	if err := cursor.All(ctx, &sessions); err != nil {
		return nil, err
	}
	return sessions, nil
}

func (r *Repository) CreateSession(ctx context.Context, session *domain.Session) error {
	if session.ID == "" {
		session.ID = uuid.NewString()
	}
	now := time.Now().UTC()
	if session.CreatedAt.IsZero() {
		session.CreatedAt = now
	}
	if session.UpdatedAt.IsZero() {
		session.UpdatedAt = now
	}
	if session.Status == "" {
		session.Status = domain.SessionStatusActive
	}
	_, err := r.sessions.InsertOne(ctx, session)
	return err
}

func (r *Repository) FindActiveSession(ctx context.Context, guildID, channelID string) (*domain.Session, error) {
	filter := bson.M{"status": domain.SessionStatusActive, "guildId": guildID, "channelId": channelID}
	var session domain.Session
	if err := r.sessions.FindOne(ctx, filter).Decode(&session); err != nil {
		if err == mongo.ErrNoDocuments {
			return nil, nil
		}
		return nil, err
	}
	return &session, nil
}

func (r *Repository) ListActiveSessions(ctx context.Context) ([]domain.Session, error) {
	cursor, err := r.sessions.Find(ctx, bson.M{"status": domain.SessionStatusActive})
	if err != nil {
		return nil, err
	}
	defer cursor.Close(ctx)

	var sessions []domain.Session
	if err := cursor.All(ctx, &sessions); err != nil {
		return nil, err
	}
	return sessions, nil
}

func (r *Repository) ListActiveSessionsByGuild(ctx context.Context, guildID string) ([]domain.Session, error) {
	cursor, err := r.sessions.Find(ctx, bson.M{"status": domain.SessionStatusActive, "guildId": guildID})
	if err != nil {
		return nil, err
	}
	defer cursor.Close(ctx)

	var sessions []domain.Session
	if err := cursor.All(ctx, &sessions); err != nil {
		return nil, err
	}
	return sessions, nil
}

func (r *Repository) GetSessionByID(ctx context.Context, sessionID string) (*domain.Session, error) {
	var session domain.Session
	if err := r.sessions.FindOne(ctx, bson.M{"_id": sessionID}).Decode(&session); err != nil {
		if err == mongo.ErrNoDocuments {
			return nil, nil
		}
		return nil, err
	}
	return &session, nil
}

func (r *Repository) CloseSession(ctx context.Context, sessionID string, endedAt time.Time, endedByUserID string) error {
	_, err := r.sessions.UpdateByID(ctx, sessionID, bson.M{
		"$set": bson.M{
			"status":        domain.SessionStatusClosed,
			"endedAt":       endedAt,
			"endedByUserId": endedByUserID,
			"updatedAt":     time.Now().UTC(),
		},
	})
	return err
}

func (r *Repository) MarkSessionClosedEventPublished(ctx context.Context, sessionID string, publishedAt time.Time) error {
	_, err := r.sessions.UpdateByID(ctx, sessionID, bson.M{
		"$set": bson.M{
			"closedEventPublishedAt": publishedAt,
			"updatedAt":              time.Now().UTC(),
		},
	})
	return err
}

func (r *Repository) MarkSessionSummaryReady(ctx context.Context, sessionID, summaryChannelID, summaryMessage string, generatedAt time.Time) error {
	_, err := r.sessions.UpdateByID(ctx, sessionID, bson.M{
		"$set": bson.M{
			"summaryChannelId":   summaryChannelID,
			"summaryMessage":     summaryMessage,
			"summaryGeneratedAt": generatedAt,
			"updatedAt":          time.Now().UTC(),
		},
	})
	return err
}

func (r *Repository) ClaimSessionSummaryDelivery(ctx context.Context, sessionID string, claimedAt time.Time) (bool, error) {
	result, err := r.sessions.UpdateOne(ctx, bson.M{
		"_id":                      sessionID,
		"summaryGeneratedAt":       bson.M{"$exists": true},
		"summaryDeliveredAt":       bson.M{"$exists": false},
		"summaryDeliveryClaimedAt": bson.M{"$exists": false},
	}, bson.M{
		"$set": bson.M{
			"summaryDeliveryClaimedAt": claimedAt,
			"updatedAt":                time.Now().UTC(),
		},
	})
	if err != nil {
		return false, err
	}
	return result.MatchedCount > 0, nil
}

func (r *Repository) ReleaseSessionSummaryDeliveryClaim(ctx context.Context, sessionID string) error {
	_, err := r.sessions.UpdateByID(ctx, sessionID, bson.M{
		"$unset": bson.M{"summaryDeliveryClaimedAt": ""},
		"$set":   bson.M{"updatedAt": time.Now().UTC()},
	})
	return err
}

func (r *Repository) MarkSessionSummaryDelivered(ctx context.Context, sessionID string, deliveredAt time.Time) error {
	_, err := r.sessions.UpdateByID(ctx, sessionID, bson.M{
		"$set": bson.M{
			"summaryDeliveredAt": deliveredAt,
			"updatedAt":          time.Now().UTC(),
		},
	})
	return err
}

func (r *Repository) CreateParticipant(ctx context.Context, participant *domain.ParticipantInterval) error {
	if participant.ID == "" {
		participant.ID = uuid.NewString()
	}
	if participant.JoinedAt.IsZero() {
		participant.JoinedAt = time.Now().UTC()
	}
	participant.Active = true
	_, err := r.participants.InsertOne(ctx, participant)
	return err
}

func (r *Repository) FindActiveParticipant(ctx context.Context, sessionID, userID string) (*domain.ParticipantInterval, error) {
	filter := bson.M{"sessionId": sessionID, "userId": userID, "active": true}
	var participant domain.ParticipantInterval
	if err := r.participants.FindOne(ctx, filter).Decode(&participant); err != nil {
		if err == mongo.ErrNoDocuments {
			return nil, nil
		}
		return nil, err
	}
	return &participant, nil
}

func (r *Repository) ListActiveParticipants(ctx context.Context, sessionID string) ([]domain.ParticipantInterval, error) {
	cursor, err := r.participants.Find(ctx, bson.M{"sessionId": sessionID, "active": true})
	if err != nil {
		return nil, err
	}
	defer cursor.Close(ctx)

	var participants []domain.ParticipantInterval
	if err := cursor.All(ctx, &participants); err != nil {
		return nil, err
	}
	return participants, nil
}

func (r *Repository) ListActiveParticipantsByGuildSession(ctx context.Context, guildID, sessionID string) ([]domain.ParticipantInterval, error) {
	cursor, err := r.participants.Find(ctx, bson.M{"guildId": guildID, "sessionId": sessionID, "active": true})
	if err != nil {
		return nil, err
	}
	defer cursor.Close(ctx)

	var participants []domain.ParticipantInterval
	if err := cursor.All(ctx, &participants); err != nil {
		return nil, err
	}
	return participants, nil
}

func (r *Repository) ListParticipantsBySession(ctx context.Context, sessionID string) ([]domain.ParticipantInterval, error) {
	cursor, err := r.participants.Find(ctx, bson.M{"sessionId": sessionID})
	if err != nil {
		return nil, err
	}
	defer cursor.Close(ctx)

	var participants []domain.ParticipantInterval
	if err := cursor.All(ctx, &participants); err != nil {
		return nil, err
	}
	return participants, nil
}

func (r *Repository) ListParticipantsByGuildChannelSession(ctx context.Context, guildID, channelID, sessionID string) ([]domain.ParticipantInterval, error) {
	cursor, err := r.participants.Find(ctx, bson.M{"guildId": guildID, "channelId": channelID, "sessionId": sessionID})
	if err != nil {
		return nil, err
	}
	defer cursor.Close(ctx)

	var participants []domain.ParticipantInterval
	if err := cursor.All(ctx, &participants); err != nil {
		return nil, err
	}
	return participants, nil
}

func (r *Repository) CloseParticipant(ctx context.Context, participantID string, leftAt time.Time, durationMs int64) error {
	_, err := r.participants.UpdateByID(ctx, participantID, bson.M{
		"$set": bson.M{
			"active":     false,
			"leftAt":     leftAt,
			"durationMs": durationMs,
		},
	})
	return err
}
