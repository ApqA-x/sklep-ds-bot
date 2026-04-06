package domain

import (
	"sort"
	"time"
)

const (
	SessionStatusActive = "active"
	SessionStatusClosed = "closed"
)

type Session struct {
	ID                       string     `bson:"_id,omitempty" json:"id"`
	GuildID                  string     `bson:"guildId" json:"guildId"`
	ChannelID                string     `bson:"channelId" json:"channelId"`
	Status                   string     `bson:"status" json:"status"`
	StartedAt                time.Time  `bson:"startedAt" json:"startedAt"`
	EndedAt                  *time.Time `bson:"endedAt,omitempty" json:"endedAt,omitempty"`
	EndedByUserID            string     `bson:"endedByUserId,omitempty" json:"endedByUserId,omitempty"`
	ClosedEventPublishedAt   *time.Time `bson:"closedEventPublishedAt,omitempty" json:"closedEventPublishedAt,omitempty"`
	SummaryChannelID         string     `bson:"summaryChannelId,omitempty" json:"summaryChannelId,omitempty"`
	SummaryMessage           string     `bson:"summaryMessage,omitempty" json:"summaryMessage,omitempty"`
	SummaryGeneratedAt       *time.Time `bson:"summaryGeneratedAt,omitempty" json:"summaryGeneratedAt,omitempty"`
	SummaryDeliveryClaimedAt *time.Time `bson:"summaryDeliveryClaimedAt,omitempty" json:"summaryDeliveryClaimedAt,omitempty"`
	SummaryDeliveredAt       *time.Time `bson:"summaryDeliveredAt,omitempty" json:"summaryDeliveredAt,omitempty"`
	CreatedAt                time.Time  `bson:"createdAt" json:"createdAt"`
	UpdatedAt                time.Time  `bson:"updatedAt" json:"updatedAt"`
}

type ParticipantInterval struct {
	ID         string     `bson:"_id,omitempty" json:"id"`
	SessionID  string     `bson:"sessionId" json:"sessionId"`
	GuildID    string     `bson:"guildId" json:"guildId"`
	ChannelID  string     `bson:"channelId" json:"channelId"`
	UserID     string     `bson:"userId" json:"userId"`
	UserName   string     `bson:"userName" json:"userName"`
	JoinedAt   time.Time  `bson:"joinedAt" json:"joinedAt"`
	LeftAt     *time.Time `bson:"leftAt,omitempty" json:"leftAt,omitempty"`
	DurationMs int64      `bson:"durationMs" json:"durationMs"`
	Active     bool       `bson:"active" json:"active"`
}

type ParticipantSummary struct {
	UserID    string        `json:"userId"`
	UserName  string        `json:"userName"`
	Intervals int           `json:"intervals"`
	TotalTime time.Duration `json:"totalTime"`
}

type SessionSummary struct {
	SessionID     string               `json:"sessionId"`
	GuildID       string               `json:"guildId"`
	ChannelID     string               `json:"channelId"`
	UniqueUsers   int                  `json:"uniqueUsers"`
	TotalDuration time.Duration        `json:"totalDuration"`
	EndedByUserID string               `json:"endedByUserId"`
	Participants  []ParticipantSummary `json:"participants"`
}

func BuildSessionSummary(session Session, participants []ParticipantInterval, endedByUserID string) SessionSummary {
	byUser := make(map[string]*ParticipantSummary)
	unique := make(map[string]struct{})

	for _, participant := range participants {
		unique[participant.UserID] = struct{}{}
		summary, ok := byUser[participant.UserID]
		if !ok {
			summary = &ParticipantSummary{UserID: participant.UserID, UserName: participant.UserName}
			byUser[participant.UserID] = summary
		}
		summary.Intervals++
		duration := participant.DurationMs
		if duration == 0 && participant.LeftAt != nil {
			duration = participant.LeftAt.Sub(participant.JoinedAt).Milliseconds()
		}
		if duration < 0 {
			duration = 0
		}
		summary.TotalTime += time.Duration(duration) * time.Millisecond
	}

	items := make([]ParticipantSummary, 0, len(byUser))
	for _, item := range byUser {
		items = append(items, *item)
	}
	sort.Slice(items, func(i, j int) bool {
		if items[i].TotalTime == items[j].TotalTime {
			return items[i].UserName < items[j].UserName
		}
		return items[i].TotalTime > items[j].TotalTime
	})

	totalDuration := time.Duration(0)
	if session.EndedAt != nil {
		totalDuration = session.EndedAt.Sub(session.StartedAt)
	}
	if totalDuration < 0 {
		totalDuration = 0
	}

	return SessionSummary{
		SessionID:     session.ID,
		GuildID:       session.GuildID,
		ChannelID:     session.ChannelID,
		UniqueUsers:   len(unique),
		TotalDuration: totalDuration,
		EndedByUserID: endedByUserID,
		Participants:  items,
	}
}
